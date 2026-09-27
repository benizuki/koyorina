"""ユーザー管理 API。can_manage_users を持つ利用者だけが叩ける。"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

import core.user_store as user_store
from core.api_common import _json_error, _request_json
from core.auth import require_login, require_user_management
from core.auth_audit import list_auth_audit_logs, record_user_operation


router = APIRouter()

VALID_ROLES = {"admin", "power", "general"}


def _require_admin(request: Request) -> dict:
    user = require_login(request)
    require_user_management(request)
    return user


@router.get("/api/users")
def api_users_list(request: Request):
    _require_admin(request)
    return user_store.list_users()


@router.post("/api/users")
async def api_users_create(request: Request):
    actor = _require_admin(request)
    payload = await _request_json(request)

    email = str(payload.get("email") or "").strip().lower()
    if not email or "@" not in email:
        return _json_error("メールアドレスを正しく入力してください。", 400)
    role = str(payload.get("role") or "general").strip()
    if role not in VALID_ROLES:
        return _json_error("ロールの指定が不正です。", 400)
    if user_store.get_user(email) is not None:
        return _json_error(f"{email} は既に登録されています。", 409)

    record = user_store.upsert_user(
        email=email,
        name=str(payload.get("name") or ""),
        role=role,
        enabled=bool(payload.get("enabled", True)),
        emails=payload.get("emails") if isinstance(payload.get("emails"), list) else None,
        permissions=payload.get("permissions") if isinstance(payload.get("permissions"), dict) else None,
    )
    record_user_operation(
        actor_email=str(actor.get("email") or ""),
        event="user.create",
        target_email=email,
        method="POST",
        path="/api/users",
    )
    return JSONResponse(status_code=201, content=record)


@router.put("/api/users/{email}")
async def api_users_update(email: str, request: Request):
    actor = _require_admin(request)
    payload = await _request_json(request)

    existing = user_store.get_user(email)
    if existing is None:
        return _json_error(f"{email} は登録されていません。", 404)

    role = str(payload.get("role") or existing["role"]).strip()
    if role not in VALID_ROLES:
        return _json_error("ロールの指定が不正です。", 400)

    # 自分自身の管理権限を落とすと、管理者が誰もいなくなりうる。
    # 「締め出し」は復旧に手作業が要るので、ここで止める。
    actor_email = str(actor.get("email") or "").lower()
    if actor_email in existing["emails"]:
        new_permissions = payload.get("permissions")
        if isinstance(new_permissions, dict) and not new_permissions.get("can_manage_users", True):
            return _json_error("自分自身のユーザー管理権限は外せません。", 400)
        if not bool(payload.get("enabled", existing["enabled"])):
            return _json_error("自分自身を無効化することはできません。", 400)

    record = user_store.upsert_user(
        email=existing["email"],
        name=str(payload.get("name", existing["name"]) or ""),
        role=role,
        enabled=bool(payload.get("enabled", existing["enabled"])),
        emails=payload.get("emails") if isinstance(payload.get("emails"), list) else existing["emails"],
        permissions=payload.get("permissions") if isinstance(payload.get("permissions"), dict) else existing["permissions"],
    )
    record_user_operation(
        actor_email=actor_email,
        event="user.update",
        target_email=existing["email"],
        detail=f"role={role} enabled={record['enabled']}",
        method="PUT",
        path=f"/api/users/{existing['email']}",
    )
    return record


@router.delete("/api/users/{email}")
def api_users_delete(email: str, request: Request):
    actor = _require_admin(request)
    actor_email = str(actor.get("email") or "").lower()

    existing = user_store.get_user(email)
    if existing is None:
        return _json_error(f"{email} は登録されていません。", 404)
    if actor_email in existing["emails"]:
        return _json_error("自分自身は削除できません。", 400)

    deleted = user_store.delete_user(email)
    record_user_operation(
        actor_email=actor_email,
        event="user.delete",
        target_email=email,
        outcome="success" if deleted else "failure",
        method="DELETE",
        path=f"/api/users/{email}",
    )
    return {"ok": deleted}


@router.get("/api/auth/audit-logs")
def api_auth_audit_logs(request: Request, limit: int = 500):
    """監査ログの閲覧。生のメールアドレスを含むため管理者に限定する。"""
    _require_admin(request)
    return {"logs": list_auth_audit_logs(limit=limit)}
