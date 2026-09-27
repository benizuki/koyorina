"""Reason-bound platform support access to one tenant."""
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.core.auth import actor, get_db
from backend.core.db import Audit, SupportSession, Tenant
from backend.core.generation_client import controller_system
from backend.domain.roles import can_manage

router = APIRouter(prefix="/api/admin/support-sessions")


def aware(value):
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def active_support(db, actor_id, tenant_id, permission="inspect"):
    now = datetime.now(UTC)
    rows = db.scalars(select(SupportSession).where(
        SupportSession.actor_id == str(actor_id), SupportSession.tenant_id == str(tenant_id),
        SupportSession.revoked_at.is_(None)).order_by(SupportSession.expires_at.desc()))
    for row in rows:
        if aware(row.expires_at) > now and (permission == "inspect" or row.permission == "repair"):
            return row
    return None


def require_support(db, user, tenant_id, permission="inspect"):
    session = active_support(db, user.id, tenant_id, permission) if can_manage(user) else None
    if session is None:
        raise HTTPException(403, "対象テナントの有効なサポートセッションが必要です。")
    return session


class SessionInput(BaseModel):
    tenant_id: UUID
    permission: Literal["inspect", "repair"]
    reason: str = Field(min_length=10, max_length=500)

    @field_validator("reason")
    @classmethod
    def useful_reason(cls, value):
        if len(value.strip()) < 10:
            raise ValueError("reason is too short")
        return value.strip()


def view(row):
    return {"id": row.id, "tenant_id": row.tenant_id, "permission": row.permission,
            "reason": row.reason, "created_at": row.created_at.isoformat(),
            "expires_at": row.expires_at.isoformat(),
            "revoked_at": row.revoked_at.isoformat() if row.revoked_at else None}


@router.get("")
def list_sessions(request: Request, db: Session = Depends(get_db)):
    user = actor(request, db)
    if not can_manage(user):
        raise HTTPException(403, "管理者だけが利用できます。")
    return [view(row) for row in db.scalars(select(SupportSession).where(
        SupportSession.actor_id == user.id).order_by(SupportSession.created_at.desc()).limit(50))]


@router.post("", status_code=201)
def create_session(payload: SessionInput, request: Request, db: Session = Depends(get_db)):
    user = actor(request, db)
    if not can_manage(user):
        raise HTTPException(403, "管理者だけが利用できます。")
    tenant = db.get(Tenant, str(payload.tenant_id))
    if tenant is None or not tenant.enabled:
        raise HTTPException(404, "テナントが見つかりません。")
    now = datetime.now(UTC)
    row = SupportSession(actor_id=user.id, tenant_id=tenant.id,
                         permission=payload.permission, reason=payload.reason,
                         created_at=now, expires_at=now + timedelta(minutes=60))
    db.add(row)
    db.flush()
    db.add(Audit(actor_id=user.id, action="support.started", resource_id=row.id,
                 detail=f"tenant={tenant.id}; permission={row.permission}; reason={row.reason}"))
    db.commit()
    return view(row)


@router.delete("/{session_id}")
async def revoke_session(session_id: UUID, request: Request, db: Session = Depends(get_db)):
    user = actor(request, db)
    if not can_manage(user):
        raise HTTPException(403, "管理者だけが利用できます。")
    row = db.get(SupportSession, str(session_id))
    if row is None or row.actor_id != user.id:
        raise HTTPException(404, "サポートセッションが見つかりません。")
    if row.revoked_at is None:
        row.revoked_at = datetime.now(UTC)
        db.add(Audit(actor_id=user.id, action="support.ended", resource_id=row.id,
                     detail=f"tenant={row.tenant_id}; permission={row.permission}"))
        db.commit()
        try:
            await controller_system(request.app.state.settings, "POST", "/support/revoke",
                                    {"actor_id": user.id, "tenant_id": row.tenant_id}, timeout=55)
        except HTTPException:
            # DB permission revocation is authoritative. Idle reaping removes an unreachable Pod.
            pass
    return view(row)


async def expire_sessions(app):
    """Revoke expired access and remove its tenant worker at the same boundary."""
    now = datetime.now(UTC)
    with app.state.sessions.begin() as db:
        expired = list(db.scalars(select(SupportSession).where(
            SupportSession.revoked_at.is_(None), SupportSession.expires_at <= now)))
        targets = [(row.actor_id, row.tenant_id) for row in expired]
        for row in expired:
            row.revoked_at = now
            db.add(Audit(actor_id=row.actor_id, action="support.expired", resource_id=row.id,
                         detail=f"tenant={row.tenant_id}; permission={row.permission}"))
    for actor_id, tenant_id in targets:
        try:
            await controller_system(app.state.settings, "POST", "/support/revoke",
                                    {"actor_id": actor_id, "tenant_id": tenant_id}, timeout=55)
        except HTTPException:
            # Access is already revoked in the DB. The controller's idle reaper is the fallback.
            pass
    return len(targets)
