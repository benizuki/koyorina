"""利用者と所属組織のマスター。触れるのは管理者だけ。"""
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, EmailStr, Field
from datetime import date, datetime, time, timedelta, timezone
from sqlalchemy import delete as sa_delete, select
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from starlette.concurrency import run_in_threadpool
from typing import Literal
from backend.core import cluster as cluster_reader
from backend.core.auth import actor, get_db
from backend.core.db import (Audit, Department, GenerationJob, Project, Tenant, TenantAiSettings,
                             User, UserTenant)
from backend.core.preview_backend import backend as preview_backend
from backend.domain import tenant_ai
from backend.domain.roles import (DEFAULT_ROLE, SYSTEM_ROLES, TENANT_ROLES, admin_tenant_ids,
                                  can_manage, can_manage_tenant)

router = APIRouter(prefix="/api")


def administrator(request, db) -> User:
    user = actor(request, db)
    if not can_manage(user):
        raise HTTPException(403, "管理者だけが利用できます。")
    return user


def tenant_administrator(request, db, tenant_id) -> User:
    """システム管理者か、そのテナントのテナント管理者。"""
    user = actor(request, db)
    if not can_manage_tenant(db, user, tenant_id):
        raise HTTPException(403, "このテナントの管理者だけが利用できます。")
    return user


def department_output(department: Department) -> dict:
    return {"id": department.id, "name": department.name, "note": department.note,
            "enabled": department.enabled}


def memberships_of(db, user_id) -> list[dict]:
    """[{tenant_id, role}]。テナントIDの順。"""
    return [{"tenant_id": tenant_id, "role": role} for tenant_id, role in db.execute(
        select(UserTenant.tenant_id, UserTenant.role).where(UserTenant.user_id == str(user_id))
        .order_by(UserTenant.tenant_id))]


def user_output(user: User, department: Department | None, tenants=None) -> dict:
    tenants = list(tenants or [])
    return {"id": user.id, "email": user.email, "display_name": user.display_name,
            "google_subject": user.google_subject, "role": user.role, "enabled": user.enabled,
            "codex_enabled": user.codex_enabled,
            "department_id": user.department_id,
            "department_name": department.name if department else None,
            # 利用者はいくつのテナントにも属せる。身元はグループで1つ、ロールはテナントごと。
            "tenants": tenants,
            "tenant_ids": [item["tenant_id"] for item in tenants]}


def assign_tenants(db, user, tenants) -> None:
    """所属テナントとそこでのロールを指定どおりに揃える。存在しないテナントは受け付けない。"""
    wanted = {str(item.tenant_id): item.role for item in tenants}
    if len(wanted) != len(tenants):
        raise HTTPException(422, "同じテナントが重複しています。")
    if any(role not in TENANT_ROLES for role in wanted.values()):
        raise HTTPException(422, "テナントのロールを選び直してください。")
    if wanted:
        known = set(db.scalars(select(Tenant.id).where(Tenant.id.in_(wanted))))
        if known != set(wanted):
            raise HTTPException(422, "テナントを選び直してください。")
    current = {membership.tenant_id: membership for membership in
               db.scalars(select(UserTenant).where(UserTenant.user_id == user.id))}
    for tenant_id, role in wanted.items():
        if tenant_id in current:
            current[tenant_id].role = role
        else:
            db.add(UserTenant(user_id=user.id, tenant_id=tenant_id, role=role))
    removed = set(current) - set(wanted)
    if removed:
        db.execute(sa_delete(UserTenant).where(UserTenant.user_id == user.id,
                                               UserTenant.tenant_id.in_(removed)))


class DepartmentInput(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    note: str = Field(default="", max_length=200)
    enabled: bool = True


class TenantMembershipInput(BaseModel):
    tenant_id: UUID
    role: str = Field(default="user")


class UserInput(BaseModel):
    email: EmailStr
    display_name: str = Field(default="", max_length=80)
    # システムロール。表記ゆれを避けるため、値はここで一覧と突き合わせる。
    role: str = Field(default=DEFAULT_ROLE)
    department_id: UUID | None = None
    # 所属テナントと、そこでのロール。複数可。省略すると変更しない。
    tenants: list[TenantMembershipInput] | None = Field(default=None, max_length=500)
    google_subject: str = Field(default="", max_length=64)
    enabled: bool = True
    codex_enabled: bool = True

    def checked_role(self) -> str:
        if self.role not in SYSTEM_ROLES:
            raise HTTPException(422, "システムロールを選び直してください。")
        return self.role


@router.get("/cluster")
async def cluster(request: Request, db: Session = Depends(get_db)):
    """開発用k3sの状態。読むだけで、操作はしない。"""
    await run_in_threadpool(administrator, request, db)
    # 生成アプリのイメージ置き場は、イメージ化が未実装のため画面に出さない。
    # 設定（APP_REGISTRY_*）と domain/app_images は本番のArtifact Registry向けに残す。
    if not cluster_reader.available():
        return {"available": False, "namespaces": []}
    try:
        namespaces = cluster_reader.app_namespaces(request.app.state.settings.app_name)
        return {"available": True, **await cluster_reader.read(namespaces)}
    except Exception:
        # 例外にはトークンや内部の経路が混ざる。状態が読めないことだけ伝える。
        raise HTTPException(503, "クラスタの状態を取得できませんでした。") from None


@router.get("/cluster/{namespace}/pods/{pod}/logs")
async def pod_logs(namespace: str, pod: str, request: Request, tail: int = 400,
                   db: Session = Depends(get_db)):
    """管理者向け。対象Podの直近ログを読み取り専用で返す。"""
    await run_in_threadpool(administrator, request, db)
    if not cluster_reader.available():
        raise HTTPException(503, "Kubernetes環境の中でのみログを取得できます。")
    try:
        namespaces = cluster_reader.app_namespaces(request.app.state.settings.app_name)
        return await cluster_reader.read_logs(namespace, pod, tail, namespaces=namespaces)
    except (ValueError, LookupError) as exc:
        raise HTTPException(404, str(exc)) from None
    except Exception:
        raise HTTPException(503, "Podのログを取得できませんでした。") from None


@router.get("/cluster/network/flows")
async def network_flows(request: Request, minutes: int = Query(15, ge=5, le=60),
                        verdict: Literal["all", "allowed", "blocked", "reset"] = "all",
                        db: Session = Depends(get_db)):
    """管理者向け。Hubbleの生ログを接続先単位にまとめて返す。"""
    await run_in_threadpool(administrator, request, db)
    if not cluster_reader.available():
        return {"available": False, "window_minutes": minutes, "observed": 0,
                "totals": {"allowed": 0, "blocked": 0, "reset": 0, "unknown": 0},
                "rows": [], "truncated": False}
    try:
        return await cluster_reader.read_network_flows(minutes, verdict)
    except Exception:
        raise HTTPException(503, "通信監査の状態を取得できませんでした。") from None


@router.get("/roles")
def roles(request: Request, db: Session = Depends(get_db)):
    """選べるロール。画面の選択肢をサーバーと揃える。"""
    actor(request, db)
    return {"system": [{"id": key, "label": label} for key, label in SYSTEM_ROLES.items()],
            "tenant": [{"id": key, "label": label} for key, label in TENANT_ROLES.items()]}


def tenant_output(tenant: Tenant) -> dict:
    return {"id": tenant.id, "name": tenant.name, "note": tenant.note, "enabled": tenant.enabled}


class TenantInput(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    note: str = Field(default="", max_length=200)
    enabled: bool = True


@router.get("/tenants")
def tenant_list(request: Request, db: Session = Depends(get_db)):
    """事業単位の区分。アプリを作るときに選ぶので、開発者にも一覧は見せる。"""
    actor(request, db)
    return [tenant_output(t) for t in db.scalars(select(Tenant).order_by(Tenant.name))]


@router.post("/tenants", status_code=201)
def tenant_create(payload: TenantInput, request: Request, db: Session = Depends(get_db)):
    admin = administrator(request, db)
    if db.scalar(select(Tenant).where(Tenant.name == payload.name)):
        raise HTTPException(409, "同じ名前のテナントがあります。一覧から確認してください。")
    tenant = Tenant(name=payload.name, note=payload.note, enabled=payload.enabled)
    db.add(tenant)
    db.flush()
    db.add(Audit(actor_id=admin.id, action="tenant.created", resource_id=tenant.id))
    db.commit()
    return tenant_output(tenant)


@router.patch("/tenants/{tenant_id}")
def tenant_update(tenant_id: UUID, payload: TenantInput, request: Request,
                  db: Session = Depends(get_db)):
    admin = administrator(request, db)
    tenant = db.get(Tenant, str(tenant_id))
    if tenant is None:
        raise HTTPException(404, "テナントが見つかりません。")
    duplicate = db.scalar(select(Tenant).where(Tenant.name == payload.name, Tenant.id != tenant.id))
    if duplicate:
        raise HTTPException(409, "同じ名前のテナントがあります。一覧から確認してください。")
    tenant.name, tenant.note, tenant.enabled = payload.name, payload.note, payload.enabled
    db.add(Audit(actor_id=admin.id, action="tenant.updated", resource_id=tenant.id))
    db.commit()
    return tenant_output(tenant)


@router.delete("/tenants/{tenant_id}")
def tenant_delete(tenant_id: UUID, request: Request, db: Session = Depends(get_db)):
    """使っているテナントは消さない。アプリの区分が消えると、利用量を数えられなくなる。"""
    admin = administrator(request, db)
    tenant = db.get(Tenant, str(tenant_id))
    if tenant is None:
        raise HTTPException(404, "テナントが見つかりません。")
    if db.scalar(select(Project.id).where(Project.tenant_id == tenant.id)):
        raise HTTPException(409, "このテナントに属するアプリがあります。先に移すか、無効にしてください。")
    db.execute(sa_delete(UserTenant).where(UserTenant.tenant_id == tenant.id))
    db.delete(tenant)
    db.add(Audit(actor_id=admin.id, action="tenant.deleted", resource_id=tenant.id))
    db.commit()
    return {"status": "deleted"}


def existing_tenant(db, tenant_id) -> Tenant:
    tenant = db.get(Tenant, str(tenant_id))
    if tenant is None:
        raise HTTPException(404, "テナントが見つかりません。")
    return tenant


@router.get("/tenants/{tenant_id}/ai-settings")
def tenant_ai_settings(tenant_id: UUID, request: Request, db: Session = Depends(get_db)):
    """テナントの生成アプリが使うGemini。APIキーは設定済みかどうかだけ返す。"""
    tenant_administrator(request, db, tenant_id)
    tenant = existing_tenant(db, tenant_id)
    key = request.app.state.settings.tenant_secret_key.get_secret_value()
    return tenant_ai.visible(db.get(TenantAiSettings, tenant.id), key)


@router.put("/tenants/{tenant_id}/ai-settings")
def tenant_ai_settings_update(tenant_id: UUID, payload: tenant_ai.TenantAiInput, request: Request,
                              db: Session = Depends(get_db)):
    admin = tenant_administrator(request, db, tenant_id)
    tenant = existing_tenant(db, tenant_id)
    row = db.get(TenantAiSettings, tenant.id) or TenantAiSettings(tenant_id=tenant.id)
    key = request.app.state.settings.tenant_secret_key.get_secret_value()
    try:
        tenant_ai.apply(row, payload, key)
    except tenant_ai.TenantAiError as exc:
        raise HTTPException(422, str(exc)) from None
    db.add(row)
    # 値は監査に残さない。どの方式にしたかだけでよい。
    db.add(Audit(actor_id=admin.id, action="tenant.ai_settings_updated", resource_id=tenant.id,
                 detail=f"backend={row.backend}"))
    db.commit()
    return tenant_ai.visible(row, key)


@router.post("/tenants/{tenant_id}/ai-settings/test")
async def tenant_ai_test(tenant_id: UUID, request: Request):
    """保存済みの設定で、生成アプリと同じ経路からGeminiへ1回だけ問い合わせる。"""
    settings = request.app.state.settings

    def prepare():
        with request.app.state.sessions() as db:
            admin = tenant_administrator(request, db, tenant_id)
            tenant = existing_tenant(db, tenant_id)
            row = db.get(TenantAiSettings, tenant.id)
            return admin.id, tenant_ai.environment(row, settings.tenant_secret_key.get_secret_value())

    admin_id, (plain, secret, wif) = await run_in_threadpool(prepare)
    if not plain:
        raise HTTPException(409, "先に Gemini の設定を保存してください。")
    if not settings.preview_enabled:
        raise HTTPException(409, "プレビュー実行環境が無効のため、生成アプリと同じ経路でテストできません。")
    result = await preview_backend(settings).probe_gemini(tenant_id, plain, secret, wif)

    def record():
        with request.app.state.sessions() as db:
            # Googleの応答の文面は残さない。成否と止まった段階だけでよい。
            db.add(Audit(actor_id=admin_id, action="tenant.ai_tested", resource_id=str(tenant_id),
                         detail=f"ok={bool(result.get('ok'))}; step={result.get('step')}"))
            db.commit()
    await run_in_threadpool(record)
    return result


@router.get("/tenants/{tenant_id}/ai-settings/workload-identity")
async def tenant_workload_identity(tenant_id: UUID, request: Request):
    """GCP側で信頼を登録するための値。プレビューが名乗る身元、発行元、公開鍵（JWKS）。"""
    def check():
        with request.app.state.sessions() as db:
            tenant_administrator(request, db, tenant_id)
            existing_tenant(db, tenant_id)
    await run_in_threadpool(check)
    settings = request.app.state.settings
    if not settings.preview_enabled:
        raise HTTPException(409, "プレビュー実行環境が無効のため、Workload Identity 連携を使えません。")
    return await preview_backend(settings).workload_identity(tenant_id)


@router.get("/departments")
def department_list(request: Request, db: Session = Depends(get_db)):
    # 利用者の編集画面で選ぶため、開発者にも一覧は見せる。
    actor(request, db)
    return [department_output(d) for d in db.scalars(select(Department).order_by(Department.name))]


@router.post("/departments", status_code=201)
def department_create(payload: DepartmentInput, request: Request, db: Session = Depends(get_db)):
    admin = administrator(request, db)
    if db.scalar(select(Department).where(Department.name == payload.name)):
        raise HTTPException(409, "同じ名前の組織があります。一覧から確認してください。")
    department = Department(name=payload.name, note=payload.note, enabled=payload.enabled)
    db.add(department)
    db.flush()
    db.add(Audit(actor_id=admin.id, action="department.created", resource_id=department.id))
    db.commit()
    return department_output(department)


@router.patch("/departments/{department_id}")
def department_update(department_id: UUID, payload: DepartmentInput, request: Request,
                      db: Session = Depends(get_db)):
    admin = administrator(request, db)
    department = db.get(Department, str(department_id))
    if department is None:
        raise HTTPException(404, "組織が見つかりません。")
    duplicate = db.scalar(select(Department).where(Department.name == payload.name,
                                                   Department.id != department.id))
    if duplicate:
        raise HTTPException(409, "同じ名前の組織があります。")
    department.name, department.note, department.enabled = payload.name, payload.note, payload.enabled
    db.add(Audit(actor_id=admin.id, action="department.updated", resource_id=department.id))
    db.commit()
    return department_output(department)


@router.delete("/departments/{department_id}")
def department_delete(department_id: UUID, request: Request, db: Session = Depends(get_db)):
    admin = administrator(request, db)
    department = db.get(Department, str(department_id))
    if department is None:
        raise HTTPException(404, "組織が見つかりません。")
    # 所属している人が残ったまま消すと、誰の所属が消えたのか分からなくなる。
    if db.scalar(select(User.id).where(User.department_id == department.id)):
        raise HTTPException(409, "所属している利用者がいます。先に所属を変更してください。")
    db.delete(department)
    db.add(Audit(actor_id=admin.id, action="department.deleted", resource_id=str(department_id)))
    db.commit()
    return {"deleted": True}


@router.get("/users")
def user_list(request: Request, db: Session = Depends(get_db)):
    administrator(request, db)
    rows = db.execute(select(User, Department).join(Department, isouter=True).order_by(User.email))
    memberships = {}
    for user_id, tenant_id, role in db.execute(
            select(UserTenant.user_id, UserTenant.tenant_id, UserTenant.role)
            .order_by(UserTenant.tenant_id)):
        memberships.setdefault(user_id, []).append({"tenant_id": tenant_id, "role": role})
    return [user_output(user, department, memberships.get(user.id, []))
            for user, department in rows]


@router.post("/users", status_code=201)
def user_create(payload: UserInput, request: Request, db: Session = Depends(get_db)):
    admin = administrator(request, db)
    role = payload.checked_role()
    email = str(payload.email).lower()
    if db.scalar(select(User).where(User.email == email)):
        raise HTTPException(409, "登録済みです。一覧から状態を確認してください。")
    user = User(email=email, display_name=payload.display_name, role=role, enabled=payload.enabled,
                codex_enabled=payload.codex_enabled,
                department_id=department_of(db, payload), google_subject=payload.google_subject or None)
    db.add(user)
    db.flush()
    assign_tenants(db, user, payload.tenants or [])
    db.add(Audit(actor_id=admin.id, action="user.invited", resource_id=user.id))
    db.commit()
    return user_output(user, db.get(Department, user.department_id) if user.department_id else None,
                       memberships_of(db, user.id))


@router.patch("/users/{user_id}")
def user_update(user_id: UUID, payload: UserInput, request: Request, db: Session = Depends(get_db)):
    admin = administrator(request, db)
    role = payload.checked_role()
    user = db.get(User, str(user_id))
    if user is None:
        raise HTTPException(404, "利用者が見つかりません。")
    if user.id == admin.id and (role != "admin" or not payload.enabled):
        # 最後の管理者が自分を降ろすと、誰も管理できなくなる。
        raise HTTPException(409, "自分自身の管理権限と利用は変更できません。")
    email = str(payload.email).lower()
    if db.scalar(select(User).where(User.email == email, User.id != user.id)):
        raise HTTPException(409, "そのメールアドレスは別の利用者が使っています。")
    user.email, user.display_name, user.role, user.enabled = email, payload.display_name, role, payload.enabled
    user.codex_enabled = payload.codex_enabled
    user.department_id = department_of(db, payload)
    user.google_subject = payload.google_subject or None
    if payload.tenants is not None:
        assign_tenants(db, user, payload.tenants)
    db.add(Audit(actor_id=admin.id, action="user.updated", resource_id=user.id))
    db.commit()
    return user_output(user, db.get(Department, user.department_id) if user.department_id else None,
                       memberships_of(db, user.id))


@router.delete("/users/{user_id}")
def user_delete(user_id: UUID, request: Request, db: Session = Depends(get_db)):
    admin = administrator(request, db)
    user = db.get(User, str(user_id))
    if user is None:
        raise HTTPException(404, "利用者が見つかりません。")
    if user.id == admin.id:
        raise HTTPException(409, "自分自身は削除できません。")
    if (db.scalar(select(Project.id).where(Project.owner_id == user.id).limit(1))
            or db.scalar(select(GenerationJob.id).where(GenerationJob.owner_id == user.id).limit(1))):
        raise HTTPException(409, "作成アプリまたは生成履歴があるため削除できません。利用を停止してください。")
    db.delete(user)
    db.add(Audit(actor_id=admin.id, action="user.deleted", resource_id=str(user_id)))
    try:
        db.commit()
    except IntegrityError:
        # 確認後に関連データが追加された場合も、データを巻き添えにしない。
        db.rollback()
        raise HTTPException(409, "関連データがあるため削除できません。利用を停止してください。") from None
    return {"deleted": True}


@router.get("/ai-usage")
def ai_usage(request: Request, days: int = 30, start: date | None = None,
             end: date | None = None, db: Session = Depends(get_db)):
    """AI生成を日別・作成アプリ別・利用者別に集計する。

    システム管理者は全テナント、テナント管理者は自分が管理者のテナントのアプリの分だけ。
    """
    viewer = actor(request, db)
    # None は全テナント。テナント管理者は、管理しているテナントのアプリだけを数える。
    scope = None if can_manage(viewer) else admin_tenant_ids(db, viewer)
    if scope is not None and not scope:
        raise HTTPException(403, "管理者だけが利用できます。")
    jst = timezone(timedelta(hours=9))
    today = datetime.now(jst).date()
    end = end or today
    start = start or end - timedelta(days=min(max(days, 1), 365) - 1)
    if start > end or (end - start).days >= 365:
        raise HTTPException(422, "集計期間は開始日から終了日まで365日以内で指定してください。")
    since = datetime.combine(start, time.min, jst).astimezone(timezone.utc)
    until = datetime.combine(end + timedelta(days=1), time.min, jst).astimezone(timezone.utc)
    users = list(db.scalars(select(User).order_by(User.email)))
    jobs = list(db.scalars(select(GenerationJob).where(
        GenerationJob.created_at >= since, GenerationJob.created_at < until,
        GenerationJob.source_type == "managed_codex")))
    projects = {project.id: project for project in db.scalars(select(Project))}
    if scope is not None:
        jobs = [job for job in jobs if job.project_id in projects and projects[job.project_id].tenant_id in scope]
    provider_names = ("codex", "gemini", "antigravity", "openai_compatible", "claude")
    rows = {user.id: {"user_id": user.id, "email": user.email,
            "display_name": user.display_name, "codex_enabled": user.codex_enabled,
            "requests": 0, "succeeded": 0, "failed": 0, "codex_requests": 0,
            "gemini_requests": 0, "antigravity_requests": 0, "openai_compatible_requests": 0,
            "claude_requests": 0, "input_tokens": 0, "output_tokens": 0,
            "cached_tokens": 0, "total_tokens": 0, "tokenized_requests": 0,
            "duration_seconds": 0.0}
            for user in users}
    providers = {name: {"provider": name, "requests": 0, "succeeded": 0, "failed": 0,
                "input_tokens": 0, "output_tokens": 0, "cached_tokens": 0,
                "total_tokens": 0, "tokenized_requests": 0, "duration_seconds": 0.0}
                for name in provider_names}
    days_by_date = {start + timedelta(days=offset): 0 for offset in range((end - start).days + 1)}
    project_rows = {}
    # 事業単位で数える。アプリ→テナントの紐付けから出す。
    tenants = {tenant.id: tenant.name for tenant in db.scalars(select(Tenant))}
    tenant_rows = {}

    def duration(job: GenerationJob) -> float:
        created, updated = job.created_at, job.updated_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=timezone.utc)
        return max(0.0, (updated - created).total_seconds())

    for job in jobs:
        row = rows.get(job.owner_id)
        if row is None:
            continue
        elapsed = duration(job)
        row["requests"] += 1
        row["succeeded"] += int(job.status == "generated")
        row["failed"] += int(job.status == "failed")
        row["duration_seconds"] += elapsed
        provider = job.provider if job.provider in provider_names else None
        if provider:
            row[job.provider + "_requests"] += 1
            provider_row = providers[provider]
            provider_row["requests"] += 1
            provider_row["succeeded"] += int(job.status == "generated")
            provider_row["failed"] += int(job.status == "failed")
            provider_row["duration_seconds"] += elapsed
        if job.total_tokens is not None:
            row["tokenized_requests"] += 1
            for field in ("input_tokens", "output_tokens", "cached_tokens", "total_tokens"):
                row[field] += getattr(job, field) or 0
                if provider:
                    providers[provider][field] += getattr(job, field) or 0
            if provider:
                providers[provider]["tokenized_requests"] += 1
        created = job.created_at.replace(tzinfo=timezone.utc) if job.created_at.tzinfo is None else job.created_at
        day = created.astimezone(jst).date()
        if day in days_by_date:
            days_by_date[day] += 1
        project = projects.get(job.project_id)
        project_row = project_rows.setdefault(job.project_id, {"project_id": job.project_id,
            "project_name": project.name if project else "削除済みのアプリ", "requests": 0,
            "failed": 0, "input_tokens": 0, "output_tokens": 0, "total_tokens": 0,
            "tokenized_requests": 0, "duration_seconds": 0.0})
        project_row["requests"] += 1
        project_row["failed"] += int(job.status == "failed")
        project_row["duration_seconds"] += elapsed
        if job.total_tokens is not None:
            project_row["tokenized_requests"] += 1
            for field in ("input_tokens", "output_tokens", "total_tokens"):
                project_row[field] += getattr(job, field) or 0
        # 区分の付いていないアプリも、どこにも数えられないままにしない。
        tenant_id = project.tenant_id if project else None
        tenant_row = tenant_rows.setdefault(tenant_id, {"tenant_id": tenant_id,
            "tenant_name": tenants.get(tenant_id, "未分類"), "projects": set(), "requests": 0,
            "failed": 0, "input_tokens": 0, "output_tokens": 0, "total_tokens": 0,
            "tokenized_requests": 0, "duration_seconds": 0.0})
        tenant_row["projects"].add(job.project_id)
        tenant_row["requests"] += 1
        tenant_row["failed"] += int(job.status == "failed")
        tenant_row["duration_seconds"] += elapsed
        if job.total_tokens is not None:
            tenant_row["tokenized_requests"] += 1
            for field in ("input_tokens", "output_tokens", "total_tokens"):
                tenant_row[field] += getattr(job, field) or 0
    result = list(rows.values())
    if scope is not None:
        # 他のテナントでしか使っていない利用者は出さない。
        result = [row for row in result if row["requests"]]
    totals = {key: sum(row[key] for row in result) for key in
              ("requests", "succeeded", "failed", "codex_requests", "gemini_requests", "antigravity_requests",
               "openai_compatible_requests", "claude_requests",
               "input_tokens", "output_tokens", "cached_tokens", "total_tokens",
               "tokenized_requests", "duration_seconds")}
    today_since = datetime.combine(today, time.min, jst).astimezone(timezone.utc)
    today_until = today_since + timedelta(days=1)
    today_requests = sum(1 for job in db.scalars(select(GenerationJob).where(
        GenerationJob.created_at >= today_since, GenerationJob.created_at < today_until,
        GenerationJob.source_type == "managed_codex"))
        if scope is None or (job.project_id in projects and projects[job.project_id].tenant_id in scope))
    return {"days": (end - start).days + 1, "since": since.isoformat(),
            "start": start.isoformat(), "end": end.isoformat(), "today_requests": today_requests,
            "daily": [{"date": day.isoformat(), "requests": count}
                      for day, count in days_by_date.items()],
            "totals": totals,
            "providers": [providers[name] for name in provider_names],
            "tenants": sorted(({**row, "projects": len(row["projects"])}
                               for row in tenant_rows.values()),
                              key=lambda row: (-row["requests"], row["tenant_name"])),
            "projects": sorted(project_rows.values(),
                                                   key=lambda row: (-row["requests"], row["project_name"])),
            "users": result}


def department_of(db, payload: UserInput) -> str | None:
    if payload.department_id is None:
        return None
    department = db.get(Department, str(payload.department_id))
    if department is None:
        raise HTTPException(422, "所属組織を選び直してください。")
    return department.id
