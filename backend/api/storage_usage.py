"""Platform administrator view of tenant PVC and backing-node capacity."""
import asyncio
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.core.auth import actor, get_db
from backend.core.db import Audit, StorageUsageSnapshot, Tenant
from backend.core.generation_client import tenant_storage
from backend.core.preview_backend import backend as preview_backend
from backend.domain.roles import can_manage

router = APIRouter(prefix="/api/admin/storage-usage")


def administrator(request, db):
    user = actor(request, db)
    if not can_manage(user):
        raise HTTPException(403, "管理者だけがストレージ利用状況を確認できます。")
    return user


def level(used, requested):
    if not requested:
        return "unknown"
    ratio = used / requested
    return "danger" if ratio >= .9 else "warning" if ratio >= .8 else "ok"


def snapshot_view(row):
    if row is None:
        return {"status": "not_measured", "claim_name": "", "requested_bytes": 0,
                "used_bytes": 0, "capacity_bytes": 0, "available_bytes": 0,
                "usage_percent": 0, "level": "unknown", "measured_at": None, "error": None}
    percent = round(row.used_bytes / row.requested_bytes * 100, 1) if row.requested_bytes else 0
    return {"status": row.status, "claim_name": row.claim_name,
            "requested_bytes": row.requested_bytes, "used_bytes": row.used_bytes,
            "capacity_bytes": row.capacity_bytes, "available_bytes": row.available_bytes,
            "usage_percent": percent, "level": level(row.used_bytes, row.requested_bytes),
            "measured_at": row.measured_at.isoformat(), "error": row.error}


def summary(db):
    tenants = list(db.scalars(select(Tenant).order_by(Tenant.name)))
    rows = list(db.scalars(select(StorageUsageSnapshot)))
    indexed = {(row.tenant_id, row.kind): row for row in rows}
    tenant_rows = []
    for tenant in tenants:
        generation = snapshot_view(indexed.get((tenant.id, "generation")))
        preview = snapshot_view(indexed.get((tenant.id, "preview")))
        statuses = {generation["level"], preview["level"]}
        tenant_rows.append({"tenant_id": tenant.id, "tenant_name": tenant.name,
                            "generation": generation, "preview": preview,
                            "used_bytes": generation["used_bytes"] + preview["used_bytes"],
                            "requested_bytes": generation["requested_bytes"] + preview["requested_bytes"],
                            "level": "danger" if "danger" in statuses else
                                     "warning" if "warning" in statuses else
                                     "unknown" if statuses == {"unknown"} else "ok"})
    successful = [row for row in rows if row.status == "ok" and row.capacity_bytes]
    node = {"capacity_bytes": 0, "available_bytes": 0, "used_bytes": 0,
            "usage_percent": 0, "level": "unknown"}
    if successful:
        # Directory-backed claims report the same backing filesystem. Use the lowest free reading;
        # adding them would multiply the node size by the number of tenants.
        picked = min(successful, key=lambda item: item.available_bytes)
        used = max(0, picked.capacity_bytes - picked.available_bytes)
        free_ratio = picked.available_bytes / picked.capacity_bytes
        node = {"capacity_bytes": picked.capacity_bytes, "available_bytes": picked.available_bytes,
                "used_bytes": used, "usage_percent": round(used / picked.capacity_bytes * 100, 1),
                "level": "danger" if free_ratio <= .1 else "warning" if free_ratio <= .2 else "ok"}
    timestamps = [(row.measured_at.replace(tzinfo=timezone.utc)
                   if row.measured_at.tzinfo is None else row.measured_at) for row in rows]
    measured = max(timestamps, default=None)
    return {"measured_at": measured.isoformat() if measured else None, "node": node,
            "tenants": tenant_rows,
            "totals": {"used_bytes": sum(item["used_bytes"] for item in tenant_rows),
                       "requested_bytes": sum(item["requested_bytes"] for item in tenant_rows)}}


@router.get("")
def get_usage(request: Request, db: Session = Depends(get_db)):
    administrator(request, db)
    return summary(db)


async def measure_pair(settings, tenant_id):
    async def safe(kind, work):
        try:
            return await work
        except Exception:
            return {"kind": kind, "claim_name": "", "status": "error",
                    "requested_bytes": 0, "used_bytes": 0, "capacity_bytes": 0,
                    "available_bytes": 0, "measured_at": datetime.now(timezone.utc).isoformat(),
                    "error": "計測用Podを起動できませんでした。"}
    preview = preview_backend(settings)
    return await asyncio.gather(safe("generation", tenant_storage(settings, tenant_id)),
                                safe("preview", preview.measure_storage(tenant_id)))


@router.post("")
async def refresh_usage(request: Request, db: Session = Depends(get_db)):
    admin = administrator(request, db)
    tenant_ids = list(db.scalars(select(Tenant.id).order_by(Tenant.name)))
    for tenant_id in tenant_ids:
        for value in await measure_pair(request.app.state.settings, tenant_id):
            kind = value.get("kind")
            if kind not in {"generation", "preview"}:
                continue
            row = db.get(StorageUsageSnapshot, (tenant_id, kind))
            if row is None:
                row = StorageUsageSnapshot(tenant_id=tenant_id, kind=kind, claim_name="")
                db.add(row)
            row.claim_name = str(value.get("claim_name") or "")[:253]
            for field in ("requested_bytes", "used_bytes", "capacity_bytes", "available_bytes"):
                setattr(row, field, max(0, int(value.get(field) or 0)))
            row.status = str(value.get("status") or "error")[:20]
            row.error = str(value.get("error"))[:300] if value.get("error") else None
            try:
                row.measured_at = datetime.fromisoformat(str(value.get("measured_at")))
            except (TypeError, ValueError):
                row.measured_at = datetime.now(timezone.utc)
    db.add(Audit(actor_id=admin.id, action="storage_usage.refreshed"))
    db.commit()
    return summary(db)
