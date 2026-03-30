from __future__ import annotations

import secrets
from typing import Optional

from fastapi import Cookie, Header, HTTPException

from .config import config
from .database import Database


_db: Optional[Database] = None
_active_admin_tokens: set[str] = set()
_active_portal_user_tokens: dict[str, int] = {}


def set_database(db: Database):
    global _db
    _db = db


def _extract_bearer(authorization: Optional[str]) -> str:
    if not authorization:
        raise HTTPException(status_code=401, detail="缺少 Authorization 头")
    prefix = "Bearer "
    if not authorization.startswith(prefix):
        raise HTTPException(status_code=401, detail="Authorization 必须使用 Bearer Token")
    token = authorization[len(prefix):].strip()
    if not token:
        raise HTTPException(status_code=401, detail="Token 不能为空")
    return token


async def resolve_service_api_key_token(raw_key: str, *, allow_internal: bool = True) -> dict:
    if _db is None:
        raise HTTPException(status_code=500, detail="数据库未初始化")

    normalized_key = str(raw_key or "").strip()
    if not normalized_key:
        raise HTTPException(status_code=401, detail="API Key 不能为空")

    if allow_internal and config.cluster_role == "subnode" and config.node_api_key and secrets.compare_digest(normalized_key, config.node_api_key):
        return {
            "id": -1,
            "name": "cluster_subnode_internal",
            "enabled": True,
            "quota_remaining": None,
            "quota_used": 0,
            "is_internal": True,
        }

    api_key = await _db.resolve_service_api_key(normalized_key)
    if api_key:
        if not bool(api_key["enabled"]):
            raise HTTPException(status_code=403, detail="API Key ???")
        return api_key

    portal_api_key = await _db.resolve_portal_user_api_key(normalized_key)
    if not portal_api_key:
        raise HTTPException(status_code=401, detail="API Key ??")
    if not bool(portal_api_key.get("enabled", True)):
        raise HTTPException(status_code=403, detail="API Key ???")

    user = await _db.get_portal_user(int(portal_api_key.get("portal_user_id") or 0))
    if not user:
        raise HTTPException(status_code=401, detail="API Key ???????")
    if not bool(user.get("enabled", True)):
        raise HTTPException(status_code=403, detail="?????")

    portal_api_key["portal_user_id"] = int(portal_api_key.get("portal_user_id") or 0)
    portal_api_key["portal_api_key_id"] = int(portal_api_key.get("id") or 0)
    portal_api_key["owner_type"] = "portal_user"
    return portal_api_key


async def verify_service_api_key(authorization: Optional[str] = Header(default=None)) -> dict:
    return await resolve_service_api_key_token(_extract_bearer(authorization), allow_internal=True)


def issue_admin_token() -> str:
    token = f"admin_{secrets.token_urlsafe(24)}"
    _active_admin_tokens.add(token)
    return token


def revoke_admin_token(token: str):
    _active_admin_tokens.discard(token)


def issue_portal_user_token(user_id: int) -> str:
    token = f"portal_{secrets.token_urlsafe(24)}"
    _active_portal_user_tokens[token] = int(user_id)
    return token


def revoke_portal_user_token(token: str):
    _active_portal_user_tokens.pop(token, None)


def revoke_portal_user_tokens_by_user_id(user_id: int):
    target = int(user_id)
    stale_tokens = [token for token, uid in _active_portal_user_tokens.items() if int(uid) == target]
    for token in stale_tokens:
        _active_portal_user_tokens.pop(token, None)


async def verify_portal_user_token(
    authorization: Optional[str] = Header(default=None),
    portal_session: Optional[str] = Cookie(default=None),
) -> dict:
    if _db is None:
        raise HTTPException(status_code=500, detail="数据库未初始化")

    token = ""
    if authorization:
        token = _extract_bearer(authorization)
    elif portal_session:
        token = str(portal_session).strip()

    if not token:
        raise HTTPException(status_code=401, detail="用户会话无效或已过期")

    user_id = _active_portal_user_tokens.get(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="用户会话无效或已过期")

    user = await _db.get_portal_user(int(user_id))
    if not user:
        _active_portal_user_tokens.pop(token, None)
        raise HTTPException(status_code=401, detail="用户不存在或已被删除")
    if not bool(user.get("enabled", True)):
        raise HTTPException(status_code=403, detail="用户已禁用")

    user["token"] = token
    return user


async def verify_admin_token(authorization: Optional[str] = Header(default=None)) -> str:
    token = _extract_bearer(authorization)
    if token not in _active_admin_tokens:
        raise HTTPException(status_code=401, detail="管理员会话无效或已过期")
    return token


async def verify_cluster_key(x_cluster_key: Optional[str] = Header(default=None)) -> str:
    if _db is None:
        raise HTTPException(status_code=500, detail="数据库未初始化")
    if not x_cluster_key:
        raise HTTPException(status_code=401, detail="缺少 X-Cluster-Key")
    is_valid = await _db.validate_cluster_key(x_cluster_key.strip())
    if not is_valid:
        raise HTTPException(status_code=401, detail="Cluster Key 无效")
    return x_cluster_key
