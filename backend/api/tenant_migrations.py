"""Platform-admin project moves between tenant storage boundaries."""
import asyncio
import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from backend.core.auth import actor, get_db
from backend.core.db import (AppBuild, AppPublication, Audit, GenerationJob, Project,
    PublicationGrant, Tenant, TenantMigration)
from backend.core.generation_client import controller, controller_system
from backend.core.publication_client import call as publication_call
from backend.core.preview_backend import backend
from backend.domain.roles import can_manage

router = APIRouter(prefix="/api/admin")
logger = logging.getLogger("koyorina.tenant-migrations")


class MoveInput(BaseModel):
    target_tenant_id: UUID
    reason: str = Field(min_length=10, max_length=500)
    # 旧共有PVCから、既に割り当て済みのテナントPVCへ移す保守用経路。
    legacy_source: bool = False

    @field_validator("reason")
    @classmethod
    def useful_reason(cls, value):
        if len(value.strip()) < 10:
            raise ValueError("reason is too short")
        return value.strip()


def view(row):
    return {"id": row.id, "project_id": row.project_id,
            "source_tenant_id": row.source_tenant_id, "target_tenant_id": row.target_tenant_id,
            "status": row.status, "error": row.error,
            "source_retained_until": row.source_retained_until.isoformat()
            if row.source_retained_until else None, "created_at": row.created_at.isoformat()}


async def run_move(app, migration_id):
    with app.state.sessions() as db:
        move = db.get(TenantMigration, migration_id)
        project = db.get(Project, move.project_id)
        jobs = list(db.scalars(select(GenerationJob).where(GenerationJob.project_id == project.id)))
        active = [(job.id, job.owner_id) for job in jobs if job.status in {"starting", "generating"}]
        build_ids = list(db.scalars(select(AppBuild.id).where(AppBuild.project_id == project.id)))
        has_publication = db.get(AppPublication, project.id) is not None
        body = {"migration_id": move.id, "project_id": project.id,
                "job_ids": [job.id for job in jobs],
                "source_tenant_id": move.source_tenant_id,
                "target_tenant_id": move.target_tenant_id,
                "legacy_source": move.source_tenant_id == move.target_tenant_id}
    publication_move = {"source_tenant_id": body["source_tenant_id"],
                        "target_tenant_id": body["target_tenant_id"], "build_ids": build_ids}
    rebind_attempted = False
    rollback_failed = False
    try:
        for job_id, owner_id in active:
            result = await controller(app.state.settings, owner_id, "POST",
                                      f"/jobs/{job_id}/cancel", tenant_id=body["source_tenant_id"])
            if result.get("status") != "failed":
                raise RuntimeError("generation did not stop")
        if active:
            with app.state.sessions.begin() as db:
                for job_id, _ in active:
                    job = db.get(GenerationJob, job_id)
                    job.status, job.error = "failed", "テナント移行のため停止しました。"
        if app.state.settings.preview_enabled:
            from backend.core.preview_backend import backend
            mover = backend(app.state.settings)
            await mover.stop(project.id, move.source_tenant_id)
        await controller_system(app.state.settings, "POST", "/migrations", body)
        # Preview migration must finish before the DB routing key changes.
        if app.state.settings.preview_enabled:
            migrate = getattr(mover, "migrate", None)
            if migrate:
                await migrate(body)
        if body['source_tenant_id'] != body['target_tenant_id'] and (has_publication or build_ids):
            rebind_attempted = True
            result = await publication_call(app.state.settings, 'POST',
                f'/projects/{body["project_id"]}/tenant-move', publication_move)
            if result is None:
                raise RuntimeError('publication controller does not support tenant migration')
        with app.state.sessions.begin() as db:
            move = db.get(TenantMigration, migration_id)
            project = db.get(Project, move.project_id)
            project.tenant_id = move.target_tenant_id
            for build in db.scalars(select(AppBuild).where(AppBuild.project_id == project.id)):
                build.tenant_id = move.target_tenant_id
            if move.source_tenant_id != move.target_tenant_id:
                db.execute(delete(PublicationGrant).where(PublicationGrant.project_id == project.id))
            move.status = "completed"
            move.source_retained_until = datetime.now(UTC) + timedelta(days=7)
            db.add(Audit(actor_id=move.actor_id, action="project.tenant_migrated",
                         resource_id=project.id,
                         detail=f"migration={move.id}; grants_cleared={move.source_tenant_id != move.target_tenant_id}; reason={move.reason}"))
    except Exception:  # noqa: BLE001 - background task must leave the DB on the source boundary
        logger.exception("tenant_migration_failed migration=%s", migration_id)
        if rebind_attempted:
            try:
                await publication_call(app.state.settings, 'POST',
                    f'/projects/{body["project_id"]}/tenant-move',
                    {"source_tenant_id": body["target_tenant_id"],
                     "target_tenant_id": body["source_tenant_id"], "build_ids": build_ids})
            except Exception:
                rollback_failed = True
                logger.exception("tenant_migration_publication_rollback_failed migration=%s", migration_id)
        with app.state.sessions.begin() as db:
            move = db.get(TenantMigration, migration_id)
            move.status = "failed"
            move.error = ("公開記録の復旧を確認できませんでした。管理者が公開基盤の状態を確認してください。"
                          if rollback_failed else
                          "公開基盤の移行に失敗しました。公開アプリの停止状態と公開コントローラーを確認してください。"
                          if rebind_attempted else
                          "データコピーまたは検証に失敗しました。移行元を継続利用します。")


async def cleanup_retained_sources(app):
    """Delete verified migration sources after seven days; failed cleanup is retried."""
    now = datetime.now(UTC)
    with app.state.sessions() as db:
        due = [(row.id, row.project_id, row.source_tenant_id, row.target_tenant_id,
                [job.id for job in db.scalars(select(GenerationJob).where(
                    GenerationJob.project_id == row.project_id))])
               for row in db.scalars(select(TenantMigration).where(
                   TenantMigration.status == "completed",
                   TenantMigration.source_retained_until.is_not(None),
                   TenantMigration.source_retained_until <= now))]
    cleaned = 0
    for migration_id, project_id, source_id, target_id, jobs in due:
        body = {"migration_id": migration_id, "project_id": project_id, "job_ids": jobs,
                "source_tenant_id": source_id, "target_tenant_id": target_id,
                "legacy_source": source_id == target_id}
        try:
            await controller_system(app.state.settings, "POST", "/migrations/cleanup", body)
            if app.state.settings.preview_enabled:
                mover = backend(app.state.settings)
                cleanup = getattr(mover, "cleanup_migration", None)
                if cleanup:
                    await cleanup(body)
            with app.state.sessions.begin() as db:
                row = db.get(TenantMigration, migration_id)
                row.status = "cleaned"
                row.source_retained_until = None
                db.add(Audit(actor_id=row.actor_id, action="project.tenant_source_deleted",
                             resource_id=project_id, detail=f"migration={migration_id}"))
            cleaned += 1
        except Exception:  # noqa: BLE001 - retain source and retry after any controller/DB failure
            # Keep the recovery source and retry on the next maintenance pass.
            logger.exception("tenant_migration_cleanup_failed migration=%s", migration_id)
            continue
    # Once every current project has completed its legacy import and recovery window,
    # remove the two obsolete shared claims. Until then they remain untouched.
    with app.state.sessions() as db:
        migrated = select(TenantMigration.project_id).where(
            TenantMigration.source_tenant_id == TenantMigration.target_tenant_id,
            TenantMigration.status.in_(["cleaned", "retired"]))
        remaining = db.scalar(select(Project.id).where(Project.id.not_in(migrated)).limit(1))
        ready = db.scalar(select(TenantMigration.id).where(
            TenantMigration.source_tenant_id == TenantMigration.target_tenant_id,
            TenantMigration.status == "cleaned").limit(1))
    if remaining is None and ready is not None:
        try:
            await controller_system(app.state.settings, "DELETE", "/migrations/legacy-claim")
            if app.state.settings.preview_enabled:
                mover = backend(app.state.settings)
                remove = getattr(mover, "delete_legacy_claim", None)
                if remove:
                    await remove()
            with app.state.sessions.begin() as db:
                rows = db.scalars(select(TenantMigration).where(
                    TenantMigration.source_tenant_id == TenantMigration.target_tenant_id,
                    TenantMigration.status == "cleaned"))
                for row in rows:
                    row.status = "retired"
                db.add(Audit(action="tenant.legacy_storage_deleted",
                             detail="generation and preview shared claims"))
        except Exception:  # noqa: BLE001 - keep retrying until both controllers confirm deletion
            logger.exception("legacy_tenant_claim_cleanup_failed")
    return cleaned


@router.post("/projects/{project_id}/tenant-migrations", status_code=202)
async def start_move(project_id: UUID, payload: MoveInput, request: Request,
                     db: Session = Depends(get_db)):
    user = actor(request, db)
    if not can_manage(user):
        raise HTTPException(403, "プラットフォーム管理者だけが移行できます。")
    project = db.scalar(select(Project).where(Project.id == str(project_id)).with_for_update())
    target = db.get(Tenant, str(payload.target_tenant_id))
    if project is None or target is None or not target.enabled:
        raise HTTPException(404, "プロジェクトまたは移行先テナントが見つかりません。")
    if project.tenant_id == target.id and not payload.legacy_source:
        raise HTTPException(409, "移行先は現在のテナントです。")
    if payload.legacy_source and project.tenant_id != target.id:
        raise HTTPException(422, "旧共有領域からの移行先は現在のテナントに限ります。")
    if db.scalar(select(TenantMigration.id).where(TenantMigration.project_id == project.id,
            TenantMigration.status == "copying")):
        raise HTTPException(409, "このプロジェクトのテナント移行が進行中です。")
    if project.tenant_id != target.id:
        publication = db.get(AppPublication, project.id)
        if publication and publication.status != 'stopped':
            raise HTTPException(409, "公開アプリを停止してからテナントを移してください。")
        if db.scalar(select(AppBuild.id).where(AppBuild.project_id == project.id,
                AppBuild.status.in_(['queued', 'building', 'pushing']))):
            raise HTTPException(409, "ビルドの終了を待ってからテナントを移してください。")
        if (publication or db.scalar(select(AppBuild.id).where(AppBuild.project_id == project.id))) and not request.app.state.settings.publication_enabled:
            raise HTTPException(503, "公開基盤が無効のため、公開記録を安全に移動できません。")
    row = TenantMigration(project_id=project.id, source_tenant_id=project.tenant_id,
                          target_tenant_id=target.id, actor_id=user.id,
                          reason=payload.reason, status="copying")
    db.add(row)
    db.flush()
    db.add(Audit(actor_id=user.id, action="project.tenant_migration_started",
                 resource_id=project.id, detail=f"migration={row.id}; reason={row.reason}"))
    db.commit()
    asyncio.create_task(run_move(request.app, row.id))
    return view(row)


@router.get("/tenant-migrations/{migration_id}")
def migration_status(migration_id: UUID, request: Request, db: Session = Depends(get_db)):
    user = actor(request, db)
    if not can_manage(user):
        raise HTTPException(403, "プラットフォーム管理者だけが確認できます。")
    row = db.get(TenantMigration, str(migration_id))
    if row is None:
        raise HTTPException(404, "移行が見つかりません。")
    return view(row)
