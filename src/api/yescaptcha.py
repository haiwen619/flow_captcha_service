from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from ..core.auth import resolve_service_api_key_token
from ..core.config import config
from ..core.database import Database
from ..core.diagnostics import diag_label
from ..core.logger import debug_logger
from ..services.captcha_runtime import CaptchaRuntime
from ..services.cluster_manager import ClusterManager
from ..services.yescaptcha_manager import YesCaptchaTaskManager, YesCaptchaTaskRecord


router = APIRouter(tags=["yescaptcha-compat"])

_db: Optional[Database] = None
_runtime: Optional[CaptchaRuntime] = None
_cluster: Optional[ClusterManager] = None
_task_manager: Optional[YesCaptchaTaskManager] = None
_CLIENT_KEY_CACHE_TTL_SECONDS = 5.0
_client_key_cache: Dict[str, Dict[str, Any]] = {}
_client_key_cache_lock = asyncio.Lock()

_TASK_TYPE_MAPPING = {
    "NoCaptchaTaskProxyless": {"captcha_type": "recaptcha_v2", "enterprise": False},
    "RecaptchaV2TaskProxyless": {"captcha_type": "recaptcha_v2", "enterprise": False},
    "RecaptchaV2EnterpriseTaskProxyless": {"captcha_type": "recaptcha_v2", "enterprise": True},
    "RecaptchaV3TaskProxyless": {"captcha_type": "recaptcha_v3", "enterprise": False},
    "RecaptchaV3TaskProxylessM1": {"captcha_type": "recaptcha_v3", "enterprise": False},
    "RecaptchaV3TaskProxylessM1S7": {"captcha_type": "recaptcha_v3", "enterprise": False},
    "RecaptchaV3TaskProxylessM1S9": {"captcha_type": "recaptcha_v3", "enterprise": False},
    "RecaptchaV3EnterpriseTaskProxyless": {"captcha_type": "recaptcha_v3", "enterprise": True},
    "TurnstileTaskProxyless": {"captcha_type": "turnstile", "enterprise": False},
    "TurnstileTaskProxylessM1": {"captcha_type": "turnstile", "enterprise": False},
    "TurnstileTaskProxylessM1S1": {"captcha_type": "turnstile", "enterprise": False},
    "TurnstileTaskProxylessM1S2": {"captcha_type": "turnstile", "enterprise": False},
    "TurnstileTaskProxylessM1S3": {"captcha_type": "turnstile", "enterprise": False},
}


class YesCaptchaProtocolError(RuntimeError):
    def __init__(self, error_code: str, error_description: str, *, error_id: int = 1):
        super().__init__(error_description)
        self.error_id = max(1, int(error_id or 1))
        self.error_code = str(error_code or "ERROR_UNKNOWN").strip() or "ERROR_UNKNOWN"
        self.error_description = str(error_description or "").strip() or self.error_code


def set_dependencies(
    db: Database,
    runtime: CaptchaRuntime,
    cluster_manager: ClusterManager,
    task_manager: YesCaptchaTaskManager,
):
    global _db, _runtime, _cluster, _task_manager
    _db = db
    _runtime = runtime
    _cluster = cluster_manager
    _task_manager = task_manager
    _client_key_cache.clear()


def _ok_response(**extra: Any) -> Dict[str, Any]:
    payload: Dict[str, Any] = {"errorId": 0}
    payload.update(extra)
    return payload


def _error_response(
    error_code: str,
    error_description: str,
    *,
    error_id: int = 1,
    task_id: Optional[str] = None,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "errorId": max(1, int(error_id or 1)),
        "errorCode": str(error_code or "ERROR_UNKNOWN").strip() or "ERROR_UNKNOWN",
        "errorDescription": str(error_description or "").strip() or str(error_code or "ERROR_UNKNOWN"),
    }
    if task_id:
        payload["taskId"] = _public_task_id(task_id)
    return payload


def _public_task_id(task_id: Any) -> Any:
    text = str(task_id or "").strip()
    if text.isdigit():
        try:
            return int(text)
        except Exception:
            return text
    return text


def _owner_scope(api_key: Dict[str, Any]) -> str:
    portal_user_id = int(api_key.get("portal_user_id") or 0)
    portal_api_key_id = int(api_key.get("portal_api_key_id") or 0)
    if portal_user_id > 0:
        return f"portal:{portal_user_id}:{portal_api_key_id}"
    return f"service:{int(api_key.get('id') or 0)}"


def _resolve_owner_ids(api_key: Dict[str, Any]) -> tuple[Optional[int], Optional[int], Optional[int]]:
    portal_user_id = int(api_key.get("portal_user_id") or 0)
    portal_api_key_id = int(api_key.get("portal_api_key_id") or 0)
    if portal_user_id > 0:
        return None, portal_user_id, portal_api_key_id or None
    service_api_key_id = int(api_key.get("id") or 0)
    return (service_api_key_id if service_api_key_id > 0 else None), None, None


async def _read_json_body(request: Request) -> Dict[str, Any]:
    try:
        payload = await request.json()
    except Exception as exc:
        raise YesCaptchaProtocolError("ERROR_BAD_REQUEST", f"请求体不是有效 JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise YesCaptchaProtocolError("ERROR_BAD_REQUEST", "请求体必须是 JSON 对象")
    return payload


async def _resolve_client_key(client_key: str) -> Dict[str, Any]:
    normalized_key = str(client_key or "").strip()
    async with _client_key_cache_lock:
        cached = _client_key_cache.get(normalized_key)
        if cached and float(cached.get("expires_at") or 0) > time.monotonic():
            return dict(cached.get("api_key") or {})
        if cached is not None:
            _client_key_cache.pop(normalized_key, None)
    try:
        api_key = await resolve_service_api_key_token(normalized_key, allow_internal=False)
        async with _client_key_cache_lock:
            _client_key_cache[normalized_key] = {
                "api_key": dict(api_key),
                "expires_at": time.monotonic() + _CLIENT_KEY_CACHE_TTL_SECONDS,
            }
        return api_key
    except HTTPException as exc:
        async with _client_key_cache_lock:
            _client_key_cache.pop(normalized_key, None)
        detail = str(exc.detail or "").strip() or "API Key 无效"
        status_code = int(exc.status_code or 401)
        if status_code == 403:
            if "禁用" in detail:
                raise YesCaptchaProtocolError("ERROR_KEY_DISABLED", detail) from exc
            raise YesCaptchaProtocolError("ERROR_ACCESS_DENIED", detail) from exc
        raise YesCaptchaProtocolError("ERROR_KEY_DOES_NOT_EXIST", detail) from exc


async def _ensure_available(api_key: Dict[str, Any]):
    if _db is None:
        raise YesCaptchaProtocolError("ERROR_INTERNAL", "数据库未初始化")

    portal_user_id = int(api_key.get("portal_user_id") or 0)
    if portal_user_id > 0:
        available, message = await _db.ensure_portal_user_available(portal_user_id)
    else:
        available, message = await _db.ensure_api_key_available(int(api_key.get("id") or 0))
    if available:
        return
    normalized = str(message or "").strip()
    if "耗尽" in normalized:
        raise YesCaptchaProtocolError("ERROR_ZERO_BALANCE", normalized or "额度不足")
    if "禁用" in normalized:
        raise YesCaptchaProtocolError("ERROR_KEY_DISABLED", normalized or "API Key 已禁用")
    raise YesCaptchaProtocolError("ERROR_ACCESS_DENIED", normalized or "当前账号不可用")


async def _consume_quota(api_key: Dict[str, Any], task_id: str):
    if _db is None:
        raise YesCaptchaProtocolError("ERROR_INTERNAL", "数据库未初始化")

    portal_user_id = int(api_key.get("portal_user_id") or 0)
    if portal_user_id > 0:
        consumed, message = await _db.consume_portal_user_quota(
            portal_user_id,
            source_type="yescaptcha_task_success",
            source_ref=str(task_id or "").strip() or None,
            note=str(api_key.get("name") or "YesCaptcha API Key"),
            portal_api_key_id=int(api_key.get("portal_api_key_id") or 0) or None,
        )
    else:
        consumed, message = await _db.consume_api_key_quota(int(api_key.get("id") or 0), session_id=str(task_id or "").strip() or None)

    if consumed:
        return
    normalized = str(message or "").strip()
    if "耗尽" in normalized:
        raise YesCaptchaProtocolError("ERROR_ZERO_BALANCE", normalized or "额度不足")
    raise YesCaptchaProtocolError("ERROR_ACCESS_DENIED", normalized or "额度扣减失败")


async def _query_balance(api_key: Dict[str, Any]) -> float:
    if _db is None:
        raise YesCaptchaProtocolError("ERROR_INTERNAL", "数据库未初始化")

    portal_user_id = int(api_key.get("portal_user_id") or 0)
    if portal_user_id > 0:
        user = await _db.get_portal_user(portal_user_id)
        if not user:
            raise YesCaptchaProtocolError("ERROR_KEY_DOES_NOT_EXIST", "用户不存在")
        remaining = user.get("quota_remaining")
    else:
        fresh_api_key = await _db.get_api_key(int(api_key.get("id") or 0))
        if not fresh_api_key:
            raise YesCaptchaProtocolError("ERROR_KEY_DOES_NOT_EXIST", "API Key 不存在")
        remaining = fresh_api_key.get("quota_remaining")

    if remaining is None:
        return float(999999999)
    try:
        return float(max(0, int(remaining)))
    except Exception:
        return 0.0


def _normalize_task(task_payload: Any) -> Dict[str, Any]:
    if not isinstance(task_payload, dict):
        raise YesCaptchaProtocolError("ERROR_BAD_PARAMETERS", "task 必须是对象")

    task_type = str(task_payload.get("type") or "").strip()
    if not task_type:
        raise YesCaptchaProtocolError("ERROR_BAD_PARAMETERS", "task.type 不能为空")
    mapping = _TASK_TYPE_MAPPING.get(task_type)
    if mapping is None:
        raise YesCaptchaProtocolError("ERROR_TASK_NOT_SUPPORTED", f"当前仅支持 {', '.join(_TASK_TYPE_MAPPING.keys())}")

    website_url = str(task_payload.get("websiteURL") or "").strip()
    website_key = str(task_payload.get("websiteKey") or "").strip()
    if not website_url:
        raise YesCaptchaProtocolError("ERROR_BAD_PARAMETERS", "task.websiteURL 不能为空")
    if not website_key:
        raise YesCaptchaProtocolError("ERROR_BAD_PARAMETERS", "task.websiteKey 不能为空")

    captcha_type = str(mapping["captcha_type"])
    enterprise = bool(mapping["enterprise"])
    is_invisible_default = not captcha_type.startswith("recaptcha_v2")
    default_action = ""
    if captcha_type == "recaptcha_v2":
        default_action = "verify"
    elif captcha_type == "recaptcha_v3":
        default_action = "homepage"
    normalized = {
        "task_type": task_type,
        "website_url": website_url,
        "website_key": website_key,
        "action": str(
            task_payload.get("pageAction")
            or task_payload.get("action")
            or task_payload.get("websiteAction")
            or default_action
        ).strip()
        or default_action,
        "enterprise": enterprise,
        "captcha_type": captcha_type,
        "is_invisible": bool(task_payload.get("isInvisible", is_invisible_default)),
        "raw_task": dict(task_payload),
    }
    return normalized


def _extract_user_agent(fingerprint: Any) -> Optional[str]:
    if not isinstance(fingerprint, dict):
        return None
    candidates = [
        fingerprint.get("userAgent"),
        fingerprint.get("user_agent"),
        fingerprint.get("ua"),
        (fingerprint.get("navigator") or {}).get("userAgent") if isinstance(fingerprint.get("navigator"), dict) else None,
    ]
    for candidate in candidates:
        normalized = str(candidate or "").strip()
        if normalized:
            return normalized
    return None


async def _safe_create_job_log(**kwargs: Any):
    if _db is None:
        return
    try:
        await _db.create_job_log(**kwargs)
    except Exception as exc:
        debug_logger.log_warning(
            "[yescaptcha] create_job_log failed "
            f"status={kwargs.get('status')} session_id={kwargs.get('session_id')}: {exc}"
        )


async def _solve_custom_token(task_payload: Dict[str, Any]) -> Dict[str, Any]:
    if _runtime is None:
        raise YesCaptchaProtocolError("ERROR_INTERNAL", "运行时未初始化")

    if config.cluster_role == "master":
        if _cluster is None:
            raise YesCaptchaProtocolError("ERROR_INTERNAL", "cluster manager 未初始化")
        return await _cluster.dispatch_custom_token(
            {
                "website_url": task_payload["website_url"],
                "website_key": task_payload["website_key"],
                "action": task_payload["action"],
                "enterprise": task_payload["enterprise"],
                "captcha_type": task_payload["captcha_type"],
                "is_invisible": task_payload["is_invisible"],
            }
        )

    return await _runtime.custom_token(
        website_url=task_payload["website_url"],
        website_key=task_payload["website_key"],
        action=task_payload["action"],
        enterprise=bool(task_payload["enterprise"]),
        captcha_type=task_payload["captcha_type"],
        is_invisible=bool(task_payload["is_invisible"]),
    )


async def _process_task(task_id: str, owner_scope: str, api_key: Dict[str, Any], task_payload: Dict[str, Any]):
    if _task_manager is None:
        return

    started = time.perf_counter()
    service_api_key_id, portal_user_id, portal_api_key_id = _resolve_owner_ids(api_key)
    action_label = f"YESCAPTCHA:{task_payload['task_type']}:{task_payload['action']}"
    should_persist_job_log = not bool(api_key.get("is_internal"))

    try:
        payload = await _solve_custom_token(task_payload)
        token = str(payload.get("token") or "").strip()
        if not token:
            raise YesCaptchaProtocolError("ERROR_CAPTCHA_UNSOLVABLE", "未获取到有效 token")

        await _consume_quota(api_key, task_id)
        fingerprint = payload.get("fingerprint") if isinstance(payload.get("fingerprint"), dict) else None
        solution: Dict[str, Any] = {"token": token}
        if task_payload["captcha_type"] != "turnstile":
            solution["gRecaptchaResponse"] = token
        user_agent = _extract_user_agent(fingerprint)
        if user_agent:
            solution["userAgent"] = user_agent

        await _task_manager.mark_ready(
            task_id,
            owner_scope=owner_scope,
            solution=solution,
            metadata={
                "node_name": payload.get("node_name", config.node_name),
                "fingerprint": fingerprint,
                "cost": 0,
                "end_time": int(time.time()),
            },
        )

        if should_persist_job_log:
            await _safe_create_job_log(
                session_id=task_id,
                api_key_id=service_api_key_id,
                project_id=task_payload["website_url"],
                action=action_label,
                status="finish:success",
                error_reason=None,
                duration_ms=int((time.perf_counter() - started) * 1000),
                portal_user_id=portal_user_id,
                portal_api_key_id=portal_api_key_id,
            )
    except YesCaptchaProtocolError as exc:
        await _task_manager.mark_error(
            task_id,
            owner_scope=owner_scope,
            error_id=exc.error_id,
            error_code=exc.error_code,
            error_description=exc.error_description,
        )
        if should_persist_job_log:
            await _safe_create_job_log(
                session_id=task_id,
                api_key_id=service_api_key_id,
                project_id=task_payload["website_url"],
                action=action_label,
                status="failed",
                error_reason=exc.error_description,
                duration_ms=int((time.perf_counter() - started) * 1000),
                portal_user_id=portal_user_id,
                portal_api_key_id=portal_api_key_id,
            )
    except Exception as exc:
        debug_logger.log_warning(
            f"[yescaptcha] task failed task_id={task_id} type={task_payload.get('task_type')} {diag_label(exc)}: {exc}"
        )
        error = YesCaptchaProtocolError("ERROR_CAPTCHA_UNSOLVABLE", str(exc) or "任务执行失败")
        await _task_manager.mark_error(
            task_id,
            owner_scope=owner_scope,
            error_id=error.error_id,
            error_code=error.error_code,
            error_description=error.error_description,
        )
        if should_persist_job_log:
            await _safe_create_job_log(
                session_id=task_id,
                api_key_id=service_api_key_id,
                project_id=task_payload["website_url"],
                action=action_label,
                status="failed",
                error_reason=error.error_description,
                duration_ms=int((time.perf_counter() - started) * 1000),
                portal_user_id=portal_user_id,
                portal_api_key_id=portal_api_key_id,
            )


def _task_result_payload(record: YesCaptchaTaskRecord) -> Dict[str, Any]:
    if record.status == "processing":
        return _ok_response(taskId=_public_task_id(record.task_id), status="processing")
    if record.status == "ready":
        metadata = dict(record.metadata or {})
        payload = _ok_response(
            taskId=_public_task_id(record.task_id),
            status="ready",
            solution=dict(record.solution or {}),
            cost=metadata.get("cost", 0),
            createTime=int(record.created_at or int(time.time())),
            endTime=int(metadata.get("end_time") or record.updated_at or int(time.time())),
        )
        node_name = str(metadata.get("node_name") or "").strip()
        if node_name:
            payload["nodeName"] = node_name
        return payload
    return _error_response(
        record.error_code or "ERROR_CAPTCHA_UNSOLVABLE",
        record.error_description or "任务执行失败",
        error_id=max(1, int(record.error_id or 1)),
        task_id=record.task_id,
    )


@router.post("/createTask")
async def create_task(request: Request):
    try:
        if _task_manager is None:
            raise YesCaptchaProtocolError("ERROR_INTERNAL", "任务管理器未初始化")

        payload = await _read_json_body(request)
        client_key = str(payload.get("clientKey") or "").strip()
        if not client_key:
            raise YesCaptchaProtocolError("ERROR_KEY_DOES_NOT_EXIST", "clientKey 不能为空")

        api_key = await _resolve_client_key(client_key)
        await _ensure_available(api_key)
        task_payload = _normalize_task(payload.get("task"))
        owner_scope = _owner_scope(api_key)
        task_id = await _task_manager.create_task(
            owner_scope=owner_scope,
            task_type=task_payload["task_type"],
            metadata={"raw_task": task_payload["raw_task"]},
        )
        worker = asyncio.create_task(_process_task(task_id, owner_scope, dict(api_key), dict(task_payload)))
        await _task_manager.register_worker(task_id, worker)
        return JSONResponse(_ok_response(taskId=_public_task_id(task_id)))
    except YesCaptchaProtocolError as exc:
        return JSONResponse(_error_response(exc.error_code, exc.error_description, error_id=exc.error_id))
    except Exception as exc:
        debug_logger.log_warning(f"[yescaptcha] createTask failed {diag_label(exc)}: {exc}")
        return JSONResponse(_error_response("ERROR_INTERNAL", str(exc) or "createTask 失败"))


@router.post("/getTaskResult")
async def get_task_result(request: Request):
    try:
        if _task_manager is None:
            raise YesCaptchaProtocolError("ERROR_INTERNAL", "任务管理器未初始化")

        payload = await _read_json_body(request)
        client_key = str(payload.get("clientKey") or "").strip()
        task_id = str(payload.get("taskId") or "").strip()
        if not client_key:
            raise YesCaptchaProtocolError("ERROR_KEY_DOES_NOT_EXIST", "clientKey 不能为空")
        if not task_id:
            raise YesCaptchaProtocolError("ERROR_BAD_PARAMETERS", "taskId 不能为空")

        api_key = await _resolve_client_key(client_key)
        record = await _task_manager.get_task(task_id, owner_scope=_owner_scope(api_key))
        if record is None:
            raise YesCaptchaProtocolError("ERROR_TASK_NOT_FOUND", "taskId 不存在或无权访问")
        return JSONResponse(_task_result_payload(record))
    except YesCaptchaProtocolError as exc:
        return JSONResponse(_error_response(exc.error_code, exc.error_description, error_id=exc.error_id))
    except Exception as exc:
        debug_logger.log_warning(f"[yescaptcha] getTaskResult failed {diag_label(exc)}: {exc}")
        return JSONResponse(_error_response("ERROR_INTERNAL", str(exc) or "getTaskResult 失败"))


@router.post("/getBalance")
async def get_balance(request: Request):
    try:
        payload = await _read_json_body(request)
        client_key = str(payload.get("clientKey") or "").strip()
        if not client_key:
            raise YesCaptchaProtocolError("ERROR_KEY_DOES_NOT_EXIST", "clientKey 不能为空")
        api_key = await _resolve_client_key(client_key)
        balance = await _query_balance(api_key)
        return JSONResponse(_ok_response(balance=balance))
    except YesCaptchaProtocolError as exc:
        return JSONResponse(_error_response(exc.error_code, exc.error_description, error_id=exc.error_id))
    except Exception as exc:
        debug_logger.log_warning(f"[yescaptcha] getBalance failed {diag_label(exc)}: {exc}")
        return JSONResponse(_error_response("ERROR_INTERNAL", str(exc) or "getBalance 失败"))
