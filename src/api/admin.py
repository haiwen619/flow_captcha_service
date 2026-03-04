from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Query

from ..core.auth import issue_admin_token, revoke_admin_token, verify_admin_token
from ..core.config import config
from ..core.database import Database
from ..core.logger import debug_logger
from ..core.models import (
    ClusterNodeUpdateRequest,
    CreateApiKeyRequest,
    LoginRequest,
    UpdateAdminCredentialsRequest,
    UpdateApiKeyRequest,
    UpdateCaptchaConfigRequest,
    UpdateSystemConfigRequest,
)
from ..services.captcha_runtime import CaptchaRuntime
from ..services.cluster_manager import ClusterManager

router = APIRouter(prefix="/api/admin", tags=["admin"])

_db: Optional[Database] = None
_runtime: Optional[CaptchaRuntime] = None
_cluster: Optional[ClusterManager] = None

RESTART_REQUIRED_CONFIG_KEYS = {
    "server.host",
    "server.port",
    "storage.db_path",
    "cluster.role",
}


def _as_bool(value: Any, field_name: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value or "").strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    raise HTTPException(status_code=400, detail=f"{field_name} 必须是布尔值")


def _as_int(value: Any, field_name: str, min_value: int, max_value: int) -> int:
    try:
        iv = int(value)
    except Exception:
        raise HTTPException(status_code=400, detail=f"{field_name} 必须是整数")
    if iv < min_value or iv > max_value:
        raise HTTPException(
            status_code=400,
            detail=f"{field_name} 必须在 [{min_value}, {max_value}] 范围内",
        )
    return iv


def _as_float(value: Any, field_name: str, min_value: float, max_value: float) -> float:
    try:
        fv = float(value)
    except Exception:
        raise HTTPException(status_code=400, detail=f"{field_name} 必须是数字")
    if fv < min_value or fv > max_value:
        raise HTTPException(
            status_code=400,
            detail=f"{field_name} 必须在 [{min_value}, {max_value}] 范围内",
        )
    return fv


def _sanitize_system_config_updates(payload: Dict[str, Any]) -> Tuple[Dict[str, Dict[str, Any]], list[str]]:
    allowed_sections = {"server", "storage", "captcha", "log", "cluster"}
    updates: Dict[str, Dict[str, Any]] = {}
    changed_keys: list[str] = []

    unknown_sections = [s for s in payload.keys() if s not in allowed_sections and s != "admin"]
    if unknown_sections:
        raise HTTPException(status_code=400, detail=f"存在不支持的配置分组: {unknown_sections}")

    server_cfg = payload.get("server")
    if isinstance(server_cfg, dict):
        section: Dict[str, Any] = {}
        if "host" in server_cfg:
            host = str(server_cfg.get("host") or "").strip()
            if not host:
                raise HTTPException(status_code=400, detail="server.host 不能为空")
            section["host"] = host
            changed_keys.append("server.host")
        if "port" in server_cfg:
            section["port"] = _as_int(server_cfg.get("port"), "server.port", 1, 65535)
            changed_keys.append("server.port")
        if section:
            updates["server"] = section

    storage_cfg = payload.get("storage")
    if isinstance(storage_cfg, dict):
        section = {}
        if "db_path" in storage_cfg:
            db_path = str(storage_cfg.get("db_path") or "").strip()
            if not db_path:
                raise HTTPException(status_code=400, detail="storage.db_path 不能为空")
            section["db_path"] = db_path
            changed_keys.append("storage.db_path")
        if section:
            updates["storage"] = section

    captcha_cfg = payload.get("captcha")
    if isinstance(captcha_cfg, dict):
        section = {}
        if "browser_launch_background" in captcha_cfg:
            section["browser_launch_background"] = _as_bool(
                captcha_cfg.get("browser_launch_background"),
                "captcha.browser_launch_background",
            )
            changed_keys.append("captcha.browser_launch_background")
        if "browser_score_dom_wait_seconds" in captcha_cfg:
            section["browser_score_dom_wait_seconds"] = _as_float(
                captcha_cfg.get("browser_score_dom_wait_seconds"),
                "captcha.browser_score_dom_wait_seconds",
                1.0,
                180.0,
            )
            changed_keys.append("captcha.browser_score_dom_wait_seconds")
        if "browser_recaptcha_settle_seconds" in captcha_cfg:
            section["browser_recaptcha_settle_seconds"] = _as_float(
                captcha_cfg.get("browser_recaptcha_settle_seconds"),
                "captcha.browser_recaptcha_settle_seconds",
                0.0,
                30.0,
            )
            changed_keys.append("captcha.browser_recaptcha_settle_seconds")
        if "browser_score_test_warmup_seconds" in captcha_cfg:
            section["browser_score_test_warmup_seconds"] = _as_float(
                captcha_cfg.get("browser_score_test_warmup_seconds"),
                "captcha.browser_score_test_warmup_seconds",
                0.0,
                300.0,
            )
            changed_keys.append("captcha.browser_score_test_warmup_seconds")
        if "flow_timeout" in captcha_cfg:
            section["flow_timeout"] = _as_int(
                captcha_cfg.get("flow_timeout"),
                "captcha.flow_timeout",
                10,
                7200,
            )
            changed_keys.append("captcha.flow_timeout")
        if "upsample_timeout" in captcha_cfg:
            section["upsample_timeout"] = _as_int(
                captcha_cfg.get("upsample_timeout"),
                "captcha.upsample_timeout",
                10,
                7200,
            )
            changed_keys.append("captcha.upsample_timeout")
        if "session_ttl_seconds" in captcha_cfg:
            section["session_ttl_seconds"] = _as_int(
                captcha_cfg.get("session_ttl_seconds"),
                "captcha.session_ttl_seconds",
                60,
                86400,
            )
            changed_keys.append("captcha.session_ttl_seconds")
        if "node_name" in captcha_cfg:
            node_name = str(captcha_cfg.get("node_name") or "").strip()
            if not node_name:
                raise HTTPException(status_code=400, detail="captcha.node_name 不能为空")
            section["node_name"] = node_name
            changed_keys.append("captcha.node_name")
        if section:
            updates["captcha"] = section

    log_cfg = payload.get("log")
    if isinstance(log_cfg, dict):
        section = {}
        if "level" in log_cfg:
            level = str(log_cfg.get("level") or "").strip().upper()
            if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
                raise HTTPException(status_code=400, detail="log.level 仅支持 DEBUG/INFO/WARNING/ERROR/CRITICAL")
            section["level"] = level
            changed_keys.append("log.level")
        if section:
            updates["log"] = section

    cluster_cfg = payload.get("cluster")
    if isinstance(cluster_cfg, dict):
        section = {}
        if "role" in cluster_cfg:
            role = str(cluster_cfg.get("role") or "").strip().lower()
            if role not in {"standalone", "master", "subnode"}:
                raise HTTPException(status_code=400, detail="cluster.role 仅支持 standalone/master/subnode")
            section["role"] = role
            changed_keys.append("cluster.role")
        if "master_base_url" in cluster_cfg:
            section["master_base_url"] = str(cluster_cfg.get("master_base_url") or "").strip().rstrip("/")
            changed_keys.append("cluster.master_base_url")
        if "master_cluster_key" in cluster_cfg:
            section["master_cluster_key"] = str(cluster_cfg.get("master_cluster_key") or "").strip()
            changed_keys.append("cluster.master_cluster_key")
        if "node_public_base_url" in cluster_cfg:
            section["node_public_base_url"] = str(cluster_cfg.get("node_public_base_url") or "").strip().rstrip("/")
            changed_keys.append("cluster.node_public_base_url")
        if "node_api_key" in cluster_cfg:
            section["node_api_key"] = str(cluster_cfg.get("node_api_key") or "").strip()
            changed_keys.append("cluster.node_api_key")
        if "heartbeat_interval_seconds" in cluster_cfg:
            section["heartbeat_interval_seconds"] = _as_int(
                cluster_cfg.get("heartbeat_interval_seconds"),
                "cluster.heartbeat_interval_seconds",
                5,
                3600,
            )
            changed_keys.append("cluster.heartbeat_interval_seconds")
        if "node_weight" in cluster_cfg:
            section["node_weight"] = _as_int(
                cluster_cfg.get("node_weight"),
                "cluster.node_weight",
                1,
                10000,
            )
            changed_keys.append("cluster.node_weight")
        if "node_max_concurrency" in cluster_cfg:
            section["node_max_concurrency"] = _as_int(
                cluster_cfg.get("node_max_concurrency"),
                "cluster.node_max_concurrency",
                1,
                200,
            )
            changed_keys.append("cluster.node_max_concurrency")
        if "master_node_stale_seconds" in cluster_cfg:
            section["master_node_stale_seconds"] = _as_int(
                cluster_cfg.get("master_node_stale_seconds"),
                "cluster.master_node_stale_seconds",
                10,
                3600,
            )
            changed_keys.append("cluster.master_node_stale_seconds")
        if "master_dispatch_timeout_seconds" in cluster_cfg:
            section["master_dispatch_timeout_seconds"] = _as_int(
                cluster_cfg.get("master_dispatch_timeout_seconds"),
                "cluster.master_dispatch_timeout_seconds",
                5,
                3600,
            )
            changed_keys.append("cluster.master_dispatch_timeout_seconds")
        if section:
            updates["cluster"] = section

    return updates, changed_keys


def _build_system_config_payload(admin_profile: Dict[str, Any]) -> Dict[str, Any]:
    merged = config.get_merged_config()
    return {
        "config_path": str(config.config_path),
        "role": config.cluster_role,
        "env_overrides": config.get_active_env_overrides(),
        "config": {
            "server": merged.get("server", {}),
            "storage": merged.get("storage", {}),
            "captcha": merged.get("captcha", {}),
            "log": merged.get("log", {}),
            "cluster": merged.get("cluster", {}),
            "admin": {
                "username": admin_profile.get("username"),
                "password": "******",
            },
        },
    }


def set_dependencies(db: Database, runtime: CaptchaRuntime, cluster_manager: ClusterManager):
    global _db, _runtime, _cluster
    _db = db
    _runtime = runtime
    _cluster = cluster_manager


@router.post("/login")
async def admin_login(request: LoginRequest):
    if _db is None:
        raise HTTPException(status_code=500, detail="服务未初始化")

    ok = await _db.verify_admin_credentials(request.username, request.password)
    if not ok:
        raise HTTPException(status_code=401, detail="用户名或密码错误")

    token = issue_admin_token()
    return {
        "success": True,
        "token": token,
        "username": request.username,
        "role": config.cluster_role,
    }


@router.post("/logout")
async def admin_logout(token: str = Depends(verify_admin_token)):
    revoke_admin_token(token)
    return {"success": True}


@router.get("/profile")
async def get_admin_profile(token: str = Depends(verify_admin_token)):
    if _db is None:
        raise HTTPException(status_code=500, detail="服务未初始化")
    profile = await _db.get_admin_profile()
    return {"success": True, "profile": profile}


@router.post("/credentials")
async def update_admin_credentials(
    request: UpdateAdminCredentialsRequest,
    token: str = Depends(verify_admin_token),
):
    if _db is None:
        raise HTTPException(status_code=500, detail="服务未初始化")
    if not request.new_username and not request.new_password:
        raise HTTPException(status_code=400, detail="至少需要提供新用户名或新密码")

    ok, message, profile = await _db.update_admin_credentials(
        current_password=request.current_password,
        new_username=request.new_username,
        new_password=request.new_password,
    )
    if not ok:
        raise HTTPException(status_code=400, detail=message)
    return {"success": True, "message": message, "profile": profile}


@router.get("/system-config")
async def get_system_config(token: str = Depends(verify_admin_token)):
    if _db is None:
        raise HTTPException(status_code=500, detail="服务未初始化")
    profile = await _db.get_admin_profile()
    return {
        "success": True,
        **_build_system_config_payload(profile),
    }


@router.post("/system-config")
async def update_system_config(
    request: UpdateSystemConfigRequest,
    token: str = Depends(verify_admin_token),
):
    if _db is None:
        raise HTTPException(status_code=500, detail="服务未初始化")

    request_payload = request.model_dump(exclude_none=True)
    updates, changed_keys = _sanitize_system_config_updates(request_payload)
    if not updates:
        raise HTTPException(status_code=400, detail="没有可更新的系统配置字段")

    config.update_config_sections(updates)

    if "log.level" in changed_keys:
        debug_logger.refresh_level()

    restart_required = any(key in RESTART_REQUIRED_CONFIG_KEYS for key in changed_keys)
    message = "系统配置已保存并热重载"
    if restart_required:
        message += "；部分配置需要重启服务后完全生效"

    profile = await _db.get_admin_profile()
    return {
        "success": True,
        "message": message,
        "restart_required": restart_required,
        "changed_keys": changed_keys,
        **_build_system_config_payload(profile),
    }


@router.get("/apikeys")
async def list_api_keys(token: str = Depends(verify_admin_token)):
    if _db is None:
        raise HTTPException(status_code=500, detail="服务未初始化")
    items = await _db.list_api_keys()
    return {"success": True, "items": items}


@router.post("/apikeys")
async def create_api_key(request: CreateApiKeyRequest, token: str = Depends(verify_admin_token)):
    if _db is None:
        raise HTTPException(status_code=500, detail="服务未初始化")

    raw_key, item = await _db.create_api_key(request.name, request.quota_remaining)
    return {
        "success": True,
        "api_key": raw_key,
        "item": item,
        "message": "仅本次返回完整 API Key，请立即保存",
    }


@router.patch("/apikeys/{api_key_id}")
async def update_api_key(
    api_key_id: int,
    request: UpdateApiKeyRequest,
    token: str = Depends(verify_admin_token),
):
    if _db is None:
        raise HTTPException(status_code=500, detail="服务未初始化")

    item = await _db.update_api_key(
        api_key_id=api_key_id,
        name=request.name,
        enabled=request.enabled,
        quota_remaining=request.quota_remaining,
    )
    if not item:
        raise HTTPException(status_code=404, detail="API Key 不存在")

    return {"success": True, "item": item}


@router.get("/logs")
async def get_logs(
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    token: str = Depends(verify_admin_token),
):
    if _db is None:
        raise HTTPException(status_code=500, detail="服务未初始化")

    items = await _db.list_job_logs(limit=limit, offset=offset)
    return {"success": True, "items": items, "limit": limit, "offset": offset}


@router.get("/stats")
async def get_stats(token: str = Depends(verify_admin_token)):
    if _db is None or _runtime is None:
        raise HTTPException(status_code=500, detail="服务未初始化")

    db_stats = await _db.get_service_stats()
    runtime_stats = await _runtime.get_stats()
    cluster_stats = await _cluster.get_cluster_runtime_summary() if _cluster else {}
    return {
        "success": True,
        "db": db_stats,
        "runtime": runtime_stats,
        "cluster": cluster_stats,
    }


@router.get("/captcha-config")
async def get_captcha_config(token: str = Depends(verify_admin_token)):
    if _db is None:
        raise HTTPException(status_code=500, detail="服务未初始化")

    cfg = await _db.get_captcha_config()
    return {
        "success": True,
        "browser_proxy_enabled": cfg.browser_proxy_enabled,
        "browser_proxy_url": cfg.browser_proxy_url or "",
        "browser_count": cfg.browser_count,
    }


@router.post("/captcha-config")
async def update_captcha_config(
    request: UpdateCaptchaConfigRequest,
    token: str = Depends(verify_admin_token),
):
    if _db is None or _runtime is None:
        raise HTTPException(status_code=500, detail="服务未初始化")

    if request.browser_proxy_enabled and request.browser_proxy_url:
        from ..services.browser_captcha import validate_browser_proxy_url

        is_valid, message = validate_browser_proxy_url(request.browser_proxy_url)
        if not is_valid:
            raise HTTPException(status_code=400, detail=message)

    await _db.update_captcha_config(
        browser_proxy_enabled=request.browser_proxy_enabled,
        browser_proxy_url=request.browser_proxy_url if request.browser_proxy_enabled else None,
        browser_count=request.browser_count,
    )
    await _runtime.reload_browser_count()

    return {"success": True}


@router.get("/cluster/config")
async def get_cluster_config(token: str = Depends(verify_admin_token)):
    if _db is None:
        raise HTTPException(status_code=500, detail="服务未初始化")

    cluster_key = await _db.get_cluster_key()
    return {
        "success": True,
        "role": config.cluster_role,
        "cluster_key": cluster_key if config.cluster_role == "master" else "",
        "node_name": config.node_name,
        "master_base_url": config.cluster_master_base_url,
        "node_public_base_url": config.cluster_node_public_base_url,
        "heartbeat_interval_seconds": config.cluster_heartbeat_interval_seconds,
    }


@router.post("/cluster/config/rotate-key")
async def rotate_cluster_key(token: str = Depends(verify_admin_token)):
    if _db is None:
        raise HTTPException(status_code=500, detail="服务未初始化")
    if config.cluster_role != "master":
        raise HTTPException(status_code=400, detail="仅 master 角色可轮换 cluster key")

    new_key = await _db.rotate_cluster_key()
    return {
        "success": True,
        "cluster_key": new_key,
    }


@router.get("/cluster/nodes")
async def list_cluster_nodes(token: str = Depends(verify_admin_token)):
    if _db is None:
        raise HTTPException(status_code=500, detail="服务未初始化")
    items = await _db.list_cluster_nodes()
    return {
        "success": True,
        "items": items,
    }


@router.patch("/cluster/nodes/{node_id}")
async def update_cluster_node(
    node_id: int,
    request: ClusterNodeUpdateRequest,
    token: str = Depends(verify_admin_token),
):
    if _db is None:
        raise HTTPException(status_code=500, detail="服务未初始化")

    updated = await _db.update_cluster_node(
        node_id=node_id,
        enabled=request.enabled,
        weight=request.weight,
        max_concurrency=request.max_concurrency,
    )
    if not updated:
        raise HTTPException(status_code=404, detail="节点不存在")

    return {
        "success": True,
        "item": updated,
    }
