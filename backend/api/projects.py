import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Literal
from uuid import UUID, uuid4
from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import delete, select
from sqlalchemy.orm import Session
from backend.core.auth import actor, get_db, audit as write_audit
from backend.domain.roles import can_develop_in, can_develop_somewhere, can_manage, developer_tenant_ids, tenant_role
from backend.core.db import (Audit, GenerationJob, Project, ProjectCollaborator,
                             ProjectSession, Tenant, User, UserTenant)
from backend.core.generation_client import controller
from backend.core import gemini_client
from backend.core.gemini_client import available as gemini_available
from backend.core.gemini_purpose import draft_purpose
from backend.core.preview_backend import backend
from backend.api.support import require_support
from backend.domain.projects import ProjectInput, approve, project_tables, specification
from backend.domain.purpose_draft import PurposeDraftInput

router = APIRouter(prefix="/api/projects")
logger = logging.getLogger("uvicorn.error")
DEFAULT_TENANT_ID = "00000000-0000-4000-8000-000000000001"


def collaborator_ids(db, project_id) -> set[str]:
    return set(db.scalars(select(ProjectCollaborator.user_id)
                          .where(ProjectCollaborator.project_id == str(project_id))))


def tenant_member(db, user_id, tenant_id) -> bool:
    found = bool(db.scalar(select(UserTenant.user_id).where(
        UserTenant.user_id == str(user_id), UserTenant.tenant_id == str(tenant_id))))
    # Base.metadata.create_all users (tests and offline imports) may predate the tenant schema.
    # Production always has the default Tenant and explicit memberships.
    return found or (str(tenant_id) == DEFAULT_TENANT_ID and db.get(Tenant, DEFAULT_TENANT_ID) is None)


def may_edit(db, project, user) -> bool:
    """仕様・生成・プレビューを触れるか。オーナーと共同開発者だけ。

    管理者は見るだけ。全員のアプリを書き換えられると、誰が変えたのか辿れなくなる。
    """
    if project.owner_id == user.id and can_manage(user):
        return True
    return tenant_member(db, user.id, project.tenant_id) and (
        project.owner_id == user.id or user.id in collaborator_ids(db, project.id))


def may_read(db, project, user) -> bool:
    """管理者は運用のため全アプリを見られる。書き換えはしない。"""
    return can_manage(user) or may_edit(db, project, user)


def admin_needs_support(db, project, user) -> bool:
    """管理者でも共同開発者として明示されたアプリには通常の編集権限を適用する。

    共同開発者登録をしているのに、管理者という理由だけでサポートセッションを
    要求すると、共有開発の権限より管理者向け閲覧規則が先に効いてしまう。
    """
    return (can_manage(user) and project.owner_id != user.id
            and user.id not in collaborator_ids(db, project.id))


def may_administer(db, project, user) -> bool:
    """削除と共有設定。共有した相手にアプリごと消されると、元へ戻す手立てが無い。"""
    return can_manage(user) or (project.owner_id == user.id
                                and tenant_member(db, user.id, project.tenant_id))


# 開発セッション。心拍が途切れたら、この時間で自動的に空く。
SESSION_LIFETIME = timedelta(minutes=2)


def aware(value):
    """タイムゾーンを補う。SQLiteは timezone=True でも素の日時を返す。"""
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def live_session(db, project_id):
    """生きているセッションだけを返す。期限切れは掃除役を置かず、読むたびに無視する。"""
    held = db.get(ProjectSession, str(project_id))
    if held is None:
        return None
    return held if aware(held.expires_at) > datetime.now(timezone.utc) else None


def session_view(db, project, user):
    held = live_session(db, project.id)
    if held is None:
        return {"holder_id": None, "holder_name": None, "mine": False, "editable": True,
                "can_take_over": False, "started_at": None}
    holder = db.get(User, held.user_id)
    mine = held.user_id == user.id
    return {"holder_id": held.user_id,
            "holder_name": (holder.display_name or holder.email) if holder else held.user_id,
            "mine": mine, "editable": mine,
            # 席を外したまま戻らない人のため。オーナーと管理者だけが引き取れる。
            "can_take_over": not mine and may_administer(db, project, user),
            "started_at": aware(held.started_at).isoformat()}


def holds_session(db, project, user) -> bool:
    held = live_session(db, project.id)
    return held is None or held.user_id == user.id


def guarded(db, project_id, user, allowed, *, lock):
    query = select(Project).where(Project.id == str(project_id))
    project = db.scalar(query.with_for_update() if lock else query)
    # 権限が無いことと存在しないことを言い分けない。関係のない人へ存在を知らせない。
    if project is None or not allowed(project):
        raise HTTPException(404, "プロジェクトが見つかりません。")
    return project


def editable(db, project_id, user, *, lock=True):
    return guarded(db, project_id, user, lambda p: may_edit(db, p, user), lock=lock)


def working(db, project_id, user):
    """書き換える経路。触れる人であることに加えて、開発中の席を持っていること。

    正しさ自体は既存の仕組みが担保している（生成はPodのロック、仕様は revision の
    突き合わせ）。ここで止めるのは、作業してから弾かれる徒労を無くすため。
    """
    project = editable(db, project_id, user)
    if not holds_session(db, project, user):
        held = live_session(db, project.id)
        holder = db.get(User, held.user_id) if held else None
        name = (holder.display_name or holder.email) if holder else "他の利用者"
        raise HTTPException(409, f"{name} さんがこのアプリを開発中です。"
                                 "閲覧はできます。編集するには、その人が終えるまで待ってください。")
    return project


def administered(db, project_id, user):
    return guarded(db, project_id, user, lambda p: may_administer(db, p, user), lock=True)


def operable(db, project_id, user, *, lock=True):
    """動いているものを止める経路。触れる人に加えて、管理者も通す。

    管理者は中身を書き換えない（may_edit から外してある）。ただし、動かしっぱなしを
    片付ける手段まで無いと、オーナーが席を外した時点で誰も止められなくなる。
    プレビューは同時に動かせる数に上限があるので、1つ放置されると他の人が使えない。

    「中身を変える」と「動いているものを止める」を分けているのが肝。
    止めるのは元へ戻せる操作で、誰が変えたのかを曖昧にしない。
    """
    return guarded(db, project_id, user,
                   lambda p: may_edit(db, p, user) or may_administer(db, p, user), lock=lock)


def readable(db, project_id, user):
    return guarded(db, project_id, user, lambda p: may_read(db, p, user), lock=False)


def output(project, *, user=None, db=None):
    """画面が出し分けるための可否も返す。押せるのにエラーになる操作を見せない。"""
    # 実行規約やスキルを含めず、利用者が確認できる依頼部分だけを返す。
    from backend.domain.generation import user_generation_prompt
    try:
        prompt_spec = ProjectInput(name=project.name, purpose=project.purpose,
                                   audience=project.audience, fields=project.fields,
                                   tables=project_tables(project), requirements=project.requirements or [],
                                   generation_prompt=project.generation_prompt or "",
                                   creation_profile=project.creation_profile)
        visible_prompt = user_generation_prompt(prompt_spec)
    except ValidationError:
        # 初期版で作られた不完全な仕様も一覧自体は開けるようにする。
        visible_prompt = (project.generation_prompt or
                          f"「{project.name}」という業務アプリを作ってください。\n\n目的\n{project.purpose}")
    result = {"id": project.id, "owner_id": project.owner_id, "tenant_id": project.tenant_id,
            "name": project.name,
            "purpose": project.purpose,
            "audience": project.audience, "fields": project.fields, "status": project.status,
            "tables": project_tables(project), "requirements": project.requirements or [],
            "creation_profile": project.creation_profile,
            "generation_prompt": visible_prompt,
            "revision": project.revision, "approved_revision": project.approved_revision,
            "updated_at": project.updated_at.isoformat(), "specification": specification(project)}
    if user is not None:
        result["is_owner"] = project.owner_id == user.id
        result["can_administer"] = may_administer(db, project, user)
        if db is not None:
            result["can_edit"] = may_edit(db, project, user)
            result["collaborator_count"] = len(collaborator_ids(db, project.id))
    return result


def interview_access(request, project_id, provider: Literal["codex", "gemini"]):
    with request.app.state.sessions() as db:
        user = actor(request, db)
        project = working(db, project_id, user)
        if not can_develop_in(db, user, project.tenant_id):
            raise HTTPException(403, "このテナントでアプリを作れる利用者だけが要件を整理できます。")
        settings = request.app.state.settings
        if provider == "codex" and not settings.codex_enabled:
            raise HTTPException(403, "Codexは無効になっています。Geminiを選ぶか、ヒアリングを省略してください。")
        if provider == "codex" and not user.codex_enabled:
            raise HTTPException(403, "この利用者はCodexを使用できません。Geminiを選ぶか、ヒアリングを省略してください。")
        if provider == "gemini" and not gemini_available(settings):
            raise HTTPException(503, "Geminiの実行環境が未設定です。ヒアリングを省略できます。")
        spec = ProjectInput(name=project.name, purpose=project.purpose, audience=project.audience,
                            fields=project.fields, tables=project_tables(project),
                            requirements=project.requirements or [],
                            generation_prompt=project.generation_prompt or "",
                            creation_profile=project.creation_profile)
        # ヒアリングも依頼した人のPodで、その人の枠で動く。
        return user.id, project.tenant_id, project.revision, spec


@router.get("")
def list_projects(request: Request, db: Session = Depends(get_db)):
    user = actor(request, db)
    query = select(Project, User).join(User, Project.owner_id == User.id)
    if not can_manage(user):
        shared = select(ProjectCollaborator.project_id).where(ProjectCollaborator.user_id == user.id)
        memberships = select(UserTenant.tenant_id).where(UserTenant.user_id == user.id)
        query = query.where(Project.tenant_id.in_(memberships),
                            (Project.owner_id == user.id) | Project.id.in_(shared))
    return [{**output(project, user=user, db=db), "owner_name": owner.display_name or owner.email,
             "owner_email": owner.email}
            for project, owner in db.execute(query.order_by(Project.updated_at.desc()))]


@router.post("", status_code=201)
def create_project(payload: ProjectInput, request: Request, tenant_id: UUID | None = None,
                   db: Session = Depends(get_db)):
    user = actor(request, db)
    # 使うだけの人には作らせない。作られたアプリは誰でも使える。どのテナントで作れるかは
    # テナントごとのロールで決まる（chosen_tenant が作れるテナントだけに絞る）。
    if not can_develop_somewhere(db, user):
        raise HTTPException(403, "アプリを作れるのは開発の役割を持つ利用者だけです。")
    # テナントは仕様（ProjectInput）ではなく置き場の区分。生成への入力には混ぜない。
    project = Project(owner_id=user.id, tenant_id=chosen_tenant(db, user, tenant_id),
                      **payload.model_dump(exclude={"llm"}))
    db.add(project)
    db.flush()
    db.add(Audit(actor_id=user.id, action="project.created", resource_id=project.id))
    db.commit()
    return output(project, user=user, db=db)


@router.post("/purpose-draft")
async def create_purpose_draft(payload: PurposeDraftInput, request: Request,
                               db: Session = Depends(get_db)):
    user = actor(request, db)
    if not can_develop_somewhere(db, user):
        raise HTTPException(403, "アプリを作れる利用者だけが目的の下書きを作成できます。")
    settings = request.app.state.settings
    if not gemini_available(settings) or not gemini_client.model(settings):
        raise HTTPException(503, "目的の下書きを作るAIが設定されていません。内容は手入力できます。")
    lock = request.app.state.purpose_draft_lock
    if lock.locked():
        raise HTTPException(429, "別の目的文を作成中です。少し待ってからお試しください。")
    attempt = str(uuid4())
    db.add(Audit(actor_id=user.id, action="purpose.draft_requested", resource_id=attempt))
    db.commit()
    try:
        async with lock, asyncio.timeout(50):
            result = await draft_purpose(payload, settings)
    except Exception:
        # 認証情報や入力内容は記録せず、運用者が接続先とモデルを確認できる情報だけ残す。
        logger.exception("purpose_draft_failed attempt=%s backend=%s model=%s",
                         attempt, gemini_client.effective(settings).gemini_api_backend,
                         gemini_client.model(settings))
        db.add(Audit(actor_id=user.id, action="purpose.draft_failed", resource_id=attempt))
        db.commit()
        raise HTTPException(502, "目的の下書きを作れませんでした。内容を手入力するか、もう一度お試しください。") from None
    db.add(Audit(actor_id=user.id, action="purpose.draft_completed", resource_id=attempt))
    db.commit()
    return result.model_dump()


class RevisionInput(BaseModel):
    revision: int = Field(ge=1)


class UpdateInput(ProjectInput):
    revision: int = Field(ge=1)


@router.put("/{project_id}")
def update_project(project_id: UUID, payload: UpdateInput, request: Request, db: Session = Depends(get_db)):
    user = actor(request, db)
    project = working(db, project_id, user)
    if payload.revision != project.revision:
        raise HTTPException(409, "別の画面で更新されています。再読み込みしてください。")
    if project.status not in {"draft", "approved"}:
        raise HTTPException(409, "実行中の仕様は編集できません。")
    for key, value in payload.model_dump(exclude={"revision", "llm"}).items():
        setattr(project, key, value)
    project.revision += 1
    project.approved_revision = None
    project.status = "draft"
    db.add(Audit(actor_id=user.id, action="spec.updated", resource_id=project.id))
    db.commit()
    return output(project, user=user, db=db)


@router.post("/{project_id}/approve")
def approve_project(project_id: UUID, payload: RevisionInput, request: Request, db: Session = Depends(get_db)):
    user = actor(request, db)
    project = working(db, project_id, user)
    try:
        approve(project.revision, payload.revision, project.status)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None
    project.approved_revision = project.revision
    project.status = "approved"
    db.add(Audit(actor_id=user.id, action="spec.approved", resource_id=project.id))
    db.commit()
    return output(project, user=user, db=db)


@router.get("/{project_id}/interview")
async def interview_status(project_id: UUID, request: Request,
                           provider: Literal["codex", "gemini"] = "gemini"):
    user_id, tenant_id, _, _ = await run_in_threadpool(interview_access, request, project_id, provider)
    return await controller(request.app.state.settings, user_id, "GET",
                            f"/projects/{project_id}/interview?provider={provider}", tenant_id=tenant_id)


@router.post("/{project_id}/interview", status_code=202)
async def start_interview(project_id: UUID, request: Request,
                          provider: Literal["codex", "gemini"] = "gemini"):
    user_id, tenant_id, _, spec = await run_in_threadpool(interview_access, request, project_id, provider)
    result = await controller(request.app.state.settings, user_id, "POST",
                              f"/projects/{project_id}/interview?provider={provider}",
                              spec.model_dump(mode="json"), tenant_id=tenant_id)
    await run_in_threadpool(write_audit, request, user_id, f"spec.interview_started.{provider}", str(project_id))
    return result


class InterviewAnswers(BaseModel):
    answers: dict[str, list[str]]


@router.post("/{project_id}/interview/answer")
async def answer_interview(project_id: UUID, payload: InterviewAnswers, request: Request,
                           provider: Literal["codex", "gemini"] = "gemini"):
    user_id, tenant_id, _, _ = await run_in_threadpool(interview_access, request, project_id, provider)
    return await controller(request.app.state.settings, user_id, "POST",
                            f"/projects/{project_id}/interview/answer?provider={provider}", payload.model_dump(),
                            tenant_id=tenant_id)


@router.post("/{project_id}/interview/cancel")
async def cancel_interview(project_id: UUID, request: Request,
                           provider: Literal["codex", "gemini"] = "gemini"):
    user_id, tenant_id, _, _ = await run_in_threadpool(interview_access, request, project_id, provider)
    return await controller(request.app.state.settings, user_id, "POST",
                            f"/projects/{project_id}/interview/cancel?provider={provider}", {},
                            tenant_id=tenant_id)


@router.post("/{project_id}/interview/apply")
async def apply_interview(project_id: UUID, payload: RevisionInput, request: Request,
                          provider: Literal["codex", "gemini"] = "gemini",
                          db: Session = Depends(get_db)):
    user = actor(request, db)
    project = working(db, project_id, user)
    if provider == "codex" and not user.codex_enabled:
        raise HTTPException(403, "この利用者はCodexを使用できません。")
    if payload.revision != project.revision:
        raise HTTPException(409, "仕様が更新されています。再読み込みしてください。")
    # 通信中はDB接続を握らないため、ここで一度確定してから状態を取得する。
    user_id, tenant_id, current_revision = user.id, project.tenant_id, project.revision
    db.commit()
    state = await controller(request.app.state.settings, user_id, "GET",
                             f"/projects/{project_id}/interview?provider={provider}", tenant_id=tenant_id)
    if state.get("status") != "completed" or not state.get("result"):
        raise HTTPException(409, "ヒアリングが完了していません。")
    refined = ProjectInput.model_validate(state["result"])
    project = working(db, project_id, user)
    if project.revision != current_revision:
        raise HTTPException(409, "仕様が更新されています。再読み込みしてください。")
    for key, value in refined.model_dump().items():
        setattr(project, key, value)
    project.revision += 1
    project.approved_revision = None
    project.status = "draft"
    db.add(Audit(actor_id=user.id, action="spec.interview_applied", resource_id=project.id))
    db.commit()
    return output(project, user=user, db=db)



def tenant_choices(db, user) -> list[str]:
    """この人がアプリを置けるテナント。そのテナントでのロールが admin / developer のもの。

    システム管理者はどこへでも置ける（tenant_roles が全テナントを admin で返す）。
    """
    allowed = developer_tenant_ids(db, user)
    return sorted(db.scalars(select(Tenant.id).where(Tenant.id.in_(allowed), Tenant.enabled.is_(True))))


def chosen_tenant(db, user, requested):
    """置き場のテナントを決める。属していないテナントへは置かせない。

    区分が無いままだと、どのテナントにも数えられないアプリが残る。
    """
    allowed = tenant_choices(db, user)
    if requested is not None:
        if str(requested) not in allowed:
            raise HTTPException(403, "そのテナントにアプリを作成できません。所属とロールを確認してください。")
        return str(requested)
    if allowed:
        return allowed[0]
    if db.scalar(select(Tenant.id).limit(1)) is None:
        return DEFAULT_TENANT_ID
    raise HTTPException(409, "所属するテナントがありません。管理者に確認してください。")


class TenantChoice(BaseModel):
    tenant_id: UUID


@router.post("/{project_id}/tenant")
def move_tenant(project_id: UUID, payload: TenantChoice, request: Request,
                db: Session = Depends(get_db)):
    """Tenant moves require the audited storage migration endpoint."""
    user = actor(request, db)
    administered(db, project_id, user)
    raise HTTPException(409, "テナント変更は管理者の移行処理から実行してください。")


class CollaboratorInput(BaseModel):
    user_id: str = Field(min_length=36, max_length=36)


def collaborator_view(db, project):
    rows = db.execute(select(ProjectCollaborator, User)
                      .join(User, ProjectCollaborator.user_id == User.id)
                      .where(ProjectCollaborator.project_id == project.id)
                      .order_by(User.email)).all()
    return [{"user_id": member.id, "name": member.display_name or member.email,
             "email": member.email, "role": tenant_role(db, member, project.tenant_id) or "user",
             "added_at": link.created_at.isoformat()} for link, member in rows]


class OwnerInput(BaseModel):
    user_id: str = Field(min_length=36, max_length=36)


@router.post("/{project_id}/owner")
def transfer_owner(project_id: UUID, payload: OwnerInput, request: Request,
                   db: Session = Depends(get_db)):
    """オーナーを引き継ぐ。作業場所はアプリ単位の共有領域にあるので、持ち主の記録だけを移す。

    引き取り手のいないアプリが残ると、消すことしかできなくなる。
    """
    user = actor(request, db)
    project = administered(db, project_id, user)
    member = db.get(User, payload.user_id)
    if member is None or not member.enabled:
        raise HTTPException(404, "利用者が見つかりません。")
    if not tenant_member(db, member.id, project.tenant_id):
        raise HTTPException(409, "同じテナントに所属する利用者だけがオーナーになれます。")
    if not can_develop_in(db, member, project.tenant_id):
        raise HTTPException(409, "このテナントでアプリを開発できる利用者だけがオーナーになれます。")
    if member.id == project.owner_id:
        return output(project, user=user, db=db)
    previous = project.owner_id
    project.owner_id = member.id
    # 元のオーナーは共同開発者として残す。引き継いだ直後に触れなくなると、
    # 引き継ぎ作業そのものができない。
    if previous not in collaborator_ids(db, project.id):
        db.add(ProjectCollaborator(project_id=project.id, user_id=previous, added_by=user.id))
    # 新しいオーナーが参加者として登録されていたなら、その行は要らない。
    db.execute(delete(ProjectCollaborator).where(ProjectCollaborator.project_id == project.id,
                                                 ProjectCollaborator.user_id == member.id))
    db.add(Audit(actor_id=user.id, action="project.owner_transferred", resource_id=project.id))
    db.commit()
    return output(project, user=user, db=db)


@router.get("/{project_id}/session")
def session_status(project_id: UUID, request: Request, db: Session = Depends(get_db)):
    user = actor(request, db)
    return session_view(db, readable(db, project_id, user), user)


@router.post("/{project_id}/session")
def take_session(project_id: UUID, request: Request, takeover: bool = False,
                 db: Session = Depends(get_db)):
    """開発の席を取る、または心拍で延ばす。空いていなければ、いま誰が持つかを返す。"""
    user = actor(request, db)
    # 引き取りは管理者にも許す。session_view の can_take_over が「できる」と
    # 返しているのに、ここで弾いていた（押すと404になる組み合わせがあった）。
    project = operable(db, project_id, user)
    held = live_session(db, project.id)
    if held is not None and held.user_id != user.id:
        # 引き取れるのはオーナーと管理者だけ。作業中の人を誰でも追い出せてはいけない。
        if not (takeover and may_administer(db, project, user)):
            return session_view(db, project, user)
        db.add(Audit(actor_id=user.id, action="project.session_taken_over",
                     resource_id=project.id))
    expires = datetime.now(timezone.utc) + SESSION_LIFETIME
    stale = db.get(ProjectSession, project.id)
    if stale is None:
        db.add(ProjectSession(project_id=project.id, user_id=user.id, expires_at=expires))
    else:
        stale.user_id, stale.expires_at = user.id, expires
        if held is None or held.user_id != user.id:
            stale.started_at = datetime.now(timezone.utc)
    db.commit()
    return session_view(db, project, user)


@router.delete("/{project_id}/session")
def release_session(project_id: UUID, request: Request, db: Session = Depends(get_db)):
    """自分の席だけを空ける。他人の席は引き取り（takeover）でしか動かせない。"""
    user = actor(request, db)
    project = operable(db, project_id, user, lock=False)
    db.execute(delete(ProjectSession).where(ProjectSession.project_id == project.id,
                                            ProjectSession.user_id == user.id))
    db.commit()
    return session_view(db, project, user)


@router.get("/{project_id}/collaborators/candidates")
def collaborator_candidates(project_id: UUID, request: Request, db: Session = Depends(get_db)):
    """呼べる相手の一覧。誰を呼べるかが分からないと、共有する操作が始められない。

    返すのは名前とメールだけ。役割や所属は管理画面の領分で、ここでは要らない。
    見せるのは開発できる利用者に限り、すでに参加している人とオーナーは外す。
    """
    user = actor(request, db)
    project = administered(db, project_id, user)
    taken = collaborator_ids(db, project.id) | {project.owner_id}
    rows = db.scalars(select(User).join(UserTenant, UserTenant.user_id == User.id).where(
        User.enabled.is_(True), UserTenant.tenant_id == project.tenant_id).order_by(User.email))
    return {"candidates": [{"user_id": member.id, "name": member.display_name or member.email,
                            "email": member.email}
                           for member in rows if member.id not in taken
                           and can_develop_in(db, member, project.tenant_id)]}


@router.get("/{project_id}/collaborators")
def list_collaborators(project_id: UUID, request: Request, db: Session = Depends(get_db)):
    """参加者は、触れる人なら誰でも見える。誰と作業しているか分からないほうが危うい。"""
    user = actor(request, db)
    # 管理者も見られる。運用で「誰が触れるアプリか」を確かめる必要がある。
    project = readable(db, project_id, user)
    return {"owner_id": project.owner_id, "can_administer": may_administer(db, project, user),
            "collaborators": collaborator_view(db, project)}


@router.post("/{project_id}/collaborators", status_code=201)
def add_collaborator(project_id: UUID, payload: CollaboratorInput, request: Request,
                     db: Session = Depends(get_db)):
    user = actor(request, db)
    # 呼べるのはオーナーと管理者だけ。参加者が別の人を呼べると、オーナーの知らない
    # ところで閲覧できる範囲が広がる。
    project = administered(db, project_id, user)
    member = db.get(User, payload.user_id)
    if member is None or not member.enabled:
        raise HTTPException(404, "利用者が見つかりません。")
    if member.id == project.owner_id:
        raise HTTPException(409, "オーナーはすでにこのアプリを扱えます。")
    if not tenant_member(db, member.id, project.tenant_id):
        raise HTTPException(409, "別テナントの利用者は追加できません。")
    # 使うだけの人を入れない。触れる範囲が役割と食い違う。ロールはこのアプリのテナントのもの。
    if not can_develop_in(db, member, project.tenant_id):
        raise HTTPException(409, "このテナントでアプリを開発できる利用者だけを追加できます。")
    if payload.user_id in collaborator_ids(db, project.id):
        return {"collaborators": collaborator_view(db, project)}
    db.add(ProjectCollaborator(project_id=project.id, user_id=member.id, added_by=user.id))
    db.add(Audit(actor_id=user.id, action="project.collaborator_added", resource_id=project.id))
    db.commit()
    return {"collaborators": collaborator_view(db, project)}


@router.delete("/{project_id}/collaborators/{user_id}")
def remove_collaborator(project_id: UUID, user_id: UUID, request: Request,
                        db: Session = Depends(get_db)):
    user = actor(request, db)
    project = administered(db, project_id, user)
    db.execute(delete(ProjectCollaborator).where(ProjectCollaborator.project_id == project.id,
                                                 ProjectCollaborator.user_id == str(user_id)))
    db.add(Audit(actor_id=user.id, action="project.collaborator_removed", resource_id=project.id))
    db.commit()
    return {"collaborators": collaborator_view(db, project)}


@router.delete("/{project_id}")
async def delete_project(project_id: UUID, request: Request, db: Session = Depends(get_db)):
    """アプリを削除する。実行環境・作業場所を先に片付け、最後にDBを消す。

    先にDBを消すと、残った実行環境や作業場所を辿る手段が無くなる。
    片付けに失敗した箇所は結果に含め、成功したものとして扱わない。
    """
    user = actor(request, db)
    # 共有した相手には消させない。元へ戻す手立てが無い操作だけを分ける。
    project = administered(db, project_id, user)
    if can_manage(user) and project.owner_id != user.id:
        support = require_support(db, user, project.tenant_id, "repair")
        db.add(Audit(actor_id=user.id, action="support.repair", resource_id=project.id,
                     detail=f"session={support.id}; content=project_delete"))
    settings = request.app.state.settings
    if db.scalar(select(GenerationJob.id).where(GenerationJob.project_id == project.id,
            GenerationJob.status.in_(["starting", "generating"]))):
        raise HTTPException(409, "生成中は削除できません。完了してからやり直してください。")
    job_ids = list(db.scalars(select(GenerationJob.id).where(GenerationJob.project_id == project.id)))
    remaining = []
    if settings.preview_enabled:
        try:
            async with request.app.state.preview_lock:
                await backend(settings).discard(project.id, project.tenant_id)
        except HTTPException:
            remaining.append("プレビューの実行環境")
    if settings.codex_controller_url:
        try:
            await controller(settings, user.id, "POST", f"/projects/{project.id}/remove",
                             {"job_ids": job_ids}, tenant_id=project.tenant_id)
        except HTTPException:
            remaining.append("AppGenの作業場所")
    db.execute(delete(GenerationJob).where(GenerationJob.project_id == project.id))
    db.delete(project)
    db.add(Audit(actor_id=user.id, action="project.deleted", resource_id=project.id))
    db.commit()
    return {"deleted": True, "remaining": remaining}


@router.post("/{project_id}/reset")
async def reset_project(project_id: UUID, request: Request, db: Session = Depends(get_db)):
    """仕様とプロジェクト枠を残し、生成物・履歴・プレビューだけを初期化する。"""
    user = actor(request, db)
    project = administered(db, project_id, user)
    if db.scalar(select(GenerationJob.id).where(
            GenerationJob.project_id == project.id,
            GenerationJob.status.in_(["starting", "generating"]))):
        raise HTTPException(409, "生成中はリセットできません。生成が終わってからやり直してください。")
    settings = request.app.state.settings
    job_ids = list(db.scalars(select(GenerationJob.id).where(GenerationJob.project_id == project.id)))
    remaining = []
    if settings.preview_enabled:
        try:
            async with request.app.state.preview_lock:
                await backend(settings).discard(project.id, project.tenant_id)
        except HTTPException:
            remaining.append("プレビューの作業場所")
    if settings.codex_controller_url:
        try:
            await controller(settings, user.id, "POST", f"/projects/{project.id}/remove",
                             {"job_ids": job_ids}, tenant_id=project.tenant_id)
        except HTTPException:
            remaining.append("AppGenの作業場所")
    db.execute(delete(GenerationJob).where(GenerationJob.project_id == project.id))
    db.add(Audit(actor_id=user.id, action="project.reset", resource_id=project.id,
                 detail="generated files, history, preview workspace"))
    db.commit()
    return {"reset": True, "remaining": remaining}
