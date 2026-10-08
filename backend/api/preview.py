"""生成コードを実行環境へ渡し、動作確認するためのAPI。

実行基盤は settings の `preview_backend` で切り替える（ローカルはDocker、共有環境は
専用コントローラ）。Koyorina本体は固定の操作しか呼ばず、実行状態はコンテナ側を正とする。
DBへ「稼働中」を書き戻さない。

DBセッションは短く閉じる。実行基盤との通信は数十秒かかることがあり、その間セッションを
保持すると接続プールが尽き、同期クエリがイベントループを塞いで健全性確認まで滞る。
"""
from uuid import UUID
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool
from backend.api.generation import job_bundle
from backend.api.projects import editable, operable, readable, working
from backend.core.app_session import origin_of, url_of
from backend.core.auth import actor, audit
from backend.core.db import Audit, GenerationJob, Project, TenantAiSettings, TenantMigration
from backend.core.preview_backend import backend
from backend.domain.preview import forward_secret
from backend.domain.preview_diagnosis import diagnose
from backend.domain import preview_env, tenant_ai

router = APIRouter(prefix="/api/projects")


def enabled(settings):
    if not settings.preview_enabled:
        raise HTTPException(503, "生成アプリのプレビューは無効です。管理者に設定を依頼してください。")
    return settings


def resolve(request, project_id, job_id=None, *, readonly=False, stopping=False):
    """権限確認だけを短いセッションで行う。参照のみで行ロックは取らない。

    プレビューの実体はプロジェクト単位なので、共同開発者もそのまま扱える。
    状態とログは管理者も見られる。起動は触れる人（オーナーと共同開発者）だけ。

    止めるのは別扱いにする（stopping）。オーナーが席を外したまま動かしっぱなしだと、
    それまで誰も止められなかった。同時に動かせる数に上限があるので、1つ放置されると
    他の人が使えなくなる。管理者は中身を書き換えないが、片付けはできる。
    """
    with request.app.state.sessions() as db:
        user = actor(request, db)
        check = readable if readonly else operable if stopping else working
        project = check(db, project_id, user)
        if not readonly and not stopping and db.scalar(select(TenantMigration.id).where(
                TenantMigration.project_id == project.id, TenantMigration.status == "copying")):
            raise HTTPException(409, "テナント移行中はプレビューを変更できません。")
        job = None
        if job_id is not None:
            job = db.scalar(select(GenerationJob).where(GenerationJob.id == str(job_id),
                GenerationJob.project_id == str(project_id)))
            if job is None:
                raise HTTPException(404, "生成履歴が見つかりません。")
    return project.id, project.tenant_id, user, job


def deployment_history(request, project_id):
    with request.app.state.sessions() as db:
        jobs = {job.id: job for job in db.scalars(select(GenerationJob).where(
            GenerationJob.project_id == str(project_id)))}
        if not jobs:
            return []
        events = db.scalars(select(Audit).where(Audit.action == "preview.started",
            Audit.resource_id.in_(list(jobs))).order_by(Audit.created_at.desc()).limit(50))
        return [{"at": event.created_at.isoformat(), "job_id": event.resource_id,
                 "revision": jobs[event.resource_id].revision,
                 "instruction": jobs[event.resource_id].instruction,
                 "generated_at": jobs[event.resource_id].created_at.isoformat()}
                for event in events if event.resource_id in jobs]


def output(settings, state):
    result = {"enabled": bool(settings.preview_enabled), "state": "stopped", "url": None,
              "port": None, "job_id": None, "updated_at": None, "message": None,
              "hint": None, "evidence": None}
    if not settings.preview_enabled:
        return {**result, "message": "プレビューは無効です。"}
    result.update({key: state.get(key) for key in
                   ("state", "port", "job_id", "updated_at", "message", "hint", "evidence")})
    if result["state"] == "running":
        result["url"] = url_of(settings, state["project_id"], "preview")
    return result


async def snapshot(settings, project_id, tenant_id=None):
    if not settings.preview_enabled:
        return output(settings, {})
    runner = backend(settings)
    state = await (runner.status(project_id, tenant_id) if tenant_id is not None
                   else runner.status(project_id))
    if state.get("state") == "failed":
        # 失敗したときだけログを読む。毎回読むと実行基盤への問い合わせが増える。
        # 読めなくても状態は返す。理由が分からないことと、状態が分からないことは別。
        try:
            found = diagnose(await (runner.logs(project_id, tenant_id) if tenant_id is not None
                                    else runner.logs(project_id)))
            # ログから原因を特定できたときだけ、実行基盤の言い分を置き換える。
            # 特定できないときの決まり文句で上書きすると、「起動を待ちましたが応答が
            # ありませんでした」のような、実行基盤しか知らない事実まで消えてしまう。
            if found["evidence"] or not state.get("message"):
                state = {**state, **found}
            else:
                state = {**state, "hint": found["hint"]}
        except Exception:
            state = {**state, "hint": "実行ログを取得できませんでした。"
                                      "少し待ってから「状態を更新」を押してください。"}
    return output(settings, {**state, "project_id": project_id})


async def launch(request, settings, project_id, tenant_id, job=None, user=None):
    bundle = await job_bundle(settings, user, job, tenant_id) if job is not None else None
    extra, secret, wif = await run_in_threadpool(launch_environment, request, project_id, tenant_id)
    context = {"app_origin": origin_of(settings, project_id, "preview"), "google_client_id": settings.google_oauth_client_id,
               "admin_email": settings.bootstrap_admin_email,
               "forward_secret": forward_secret(project_id, settings.app_session_secret),
               "extra_env": extra, "secret_env": secret, "wif": wif}
    async with request.app.state.preview_lock:
        state = await backend(settings).start(project_id, bundle, job.id if job else None, context,
                                              tenant_id)
    return output(settings, {**state, "project_id": project_id})


def launch_environment(request, project_id, tenant_id):
    """テナントのGeminiの既定値に、プロジェクトの環境変数を重ねる。起動のたびに最新を読む。"""
    with request.app.state.sessions() as db:
        saved = db.scalar(select(Project.preview_env).where(Project.id == str(project_id)))
        row = db.get(TenantAiSettings, str(tenant_id)) if tenant_id else None
        tenant = tenant_ai.environment(row, request.app.state.settings.tenant_secret_key.get_secret_value())
    return tenant_ai.layered(tenant, preview_env.as_environment(saved))


def stored_environment(request, project_id) -> dict:
    """保存済みの指定を実行環境の形で返す。起動のたびに最新を読む。"""
    with request.app.state.sessions() as db:
        saved = db.scalar(select(Project.preview_env).where(Project.id == str(project_id)))
    return preview_env.as_environment(saved)


def read_environment(request, project_id):
    with request.app.state.sessions() as db:
        user = actor(request, db)
        # 秘密にした項目は visible が値を返さない。共同開発者にも名前だけが見える。
        project = editable(db, project_id, user, lock=False)
        row = db.get(TenantAiSettings, project.tenant_id)
        return inherited(row, project.preview_env) + preview_env.visible(project.preview_env)


def inherited(row, stored) -> list[dict]:
    """テナントから受け継ぐ項目。値は見せず、どの名前が届くかと、上書き済みかだけを示す。

    同じ名前をプロジェクトで指定していれば、そちらが勝つ（overridden）。
    """
    plain, _, _ = tenant_ai.environment(row, "")  # 鍵を渡さないので、秘密は開かない
    own = {item.get("name") for item in (stored or []) if isinstance(item, dict)}
    items = [{"name": name, "secret": False, "value": value, "configured": True,
              "inherited": True, "overridden": name in own} for name, value in sorted(plain.items())]
    if getattr(row, "backend", "") == "gemini_api" and getattr(row, "api_key_encrypted", None):
        items.append({"name": tenant_ai.API_KEY, "secret": True, "value": None, "configured": True,
                      "inherited": True, "overridden": tenant_ai.API_KEY in own})
    return items


def write_environment(request, project_id, entries):
    with request.app.state.sessions() as db:
        user = actor(request, db)
        project = working(db, project_id, user)
        # 受け継いだ項目は画面の一覧に並ぶが、プロジェクトの指定ではない。保存しない。
        entries = [entry for entry in entries if not (isinstance(entry, dict) and entry.get("inherited"))]
        try:
            merged = preview_env.merge(project.preview_env, preview_env.parse(entries))
        except preview_env.InvalidEnvironment as exc:
            raise HTTPException(422, str(exc)) from None
        project.preview_env = merged
        # 値は監査に残さない。秘密でない項目も、中身は記録の対象にしない。
        db.add(Audit(actor_id=user.id, action="preview.environment_updated",
                     resource_id=str(project_id)))
        db.commit()
        row = db.get(TenantAiSettings, project.tenant_id)
        return inherited(row, merged) + preview_env.visible(merged)


class EnvironmentInput(BaseModel):
    entries: list[dict] = Field(default_factory=list, max_length=preview_env.MAX_ENTRIES)


@router.get("/{project_id}/preview/env")
async def environment(project_id: UUID, request: Request):
    """秘密の項目は値を返さない。設定済みかどうかだけ伝える。"""
    return {"entries": await run_in_threadpool(read_environment, request, project_id)}


@router.put("/{project_id}/preview/env")
async def update_environment(project_id: UUID, payload: EnvironmentInput, request: Request):
    """保存するだけ。実行中のプレビューへは再起動で反映される。"""
    entries = await run_in_threadpool(write_environment, request, project_id, payload.entries)
    return {"entries": entries, "restart_required": True}


@router.get("/{project_id}/preview")
async def status(project_id: UUID, request: Request):
    identifier, tenant_id, _, _ = await run_in_threadpool(resolve, request, project_id, readonly=True)
    return await snapshot(request.app.state.settings, identifier, tenant_id)


@router.post("/{project_id}/jobs/{job_id}/preview", status_code=202)
async def start(project_id: UUID, job_id: UUID, request: Request):
    settings = enabled(request.app.state.settings)
    identifier, tenant_id, user, job = await run_in_threadpool(resolve, request, project_id, job_id)
    result = await launch(request, settings, identifier, tenant_id, job=job, user=user)
    await run_in_threadpool(audit, request, user.id, "preview.started", job.id)
    return result


@router.post("/{project_id}/preview/restart", status_code=202)
async def restart(project_id: UUID, request: Request):
    settings = enabled(request.app.state.settings)
    identifier, tenant_id, user, _ = await run_in_threadpool(resolve, request, project_id)
    job = None
    if settings.preview_backend == "controller":
        with request.app.state.sessions() as db:
            job = db.scalar(select(GenerationJob).where(
                GenerationJob.project_id == str(project_id), GenerationJob.status == "generated")
                .order_by(GenerationJob.created_at.desc()))
        if job is None:
            raise HTTPException(409, "先に生成済みのコードからプレビューを開始してください。")
    result = await launch(request, settings, identifier, tenant_id, job=job, user=user)
    await run_in_threadpool(audit, request, user.id, "preview.restarted", identifier)
    return result


@router.delete("/{project_id}/preview")
async def stop(project_id: UUID, request: Request):
    """止めるのは管理者もできる。開発の席は要らない（片付けは編集ではない）。"""
    settings = enabled(request.app.state.settings)
    identifier, tenant_id, user, _ = await run_in_threadpool(resolve, request, project_id, stopping=True)
    async with request.app.state.preview_lock:
        state = await backend(settings).stop(identifier, tenant_id)
    await run_in_threadpool(audit, request, user.id, "preview.stopped", identifier)
    return output(settings, {**state, "project_id": identifier})


@router.delete("/{project_id}/preview/workspace")
async def discard(project_id: UUID, request: Request):
    settings = enabled(request.app.state.settings)
    identifier, tenant_id, user, _ = await run_in_threadpool(resolve, request, project_id)
    async with request.app.state.preview_lock:
        state = await backend(settings).discard(identifier, tenant_id)
    await run_in_threadpool(audit, request, user.id, "preview.workspace_removed", identifier)
    return output(settings, {**state, "project_id": identifier})


@router.get("/{project_id}/deployments")
async def deployments(project_id: UUID, request: Request):
    """開発の区切りは、プレビューへ出した時点で数える。生成1回ごとではない。"""
    identifier, _, _, _ = await run_in_threadpool(resolve, request, project_id, readonly=True)
    return await run_in_threadpool(deployment_history, request, identifier)


@router.get("/{project_id}/preview/logs")
async def logs(project_id: UUID, request: Request):
    settings = enabled(request.app.state.settings)
    identifier, tenant_id, _, _ = await run_in_threadpool(resolve, request, project_id, readonly=True)
    return {"logs": await backend(settings).logs(identifier, tenant_id)}


class CommandInput(BaseModel):
    # 1行1回。複数行を許すと「貼り付けたら全部走った」が起きる。
    command: str = Field(min_length=1, max_length=2000, pattern=r"^[^\r\n]+$")


@router.post("/{project_id}/preview/exec")
async def execute(project_id: UUID, payload: CommandInput, request: Request):
    """動いているプレビューの中でコマンドを1回だけ実行する。調査のための口。

    読むだけの人には出さない（resolve の既定は working＝開発の席を持つ人）。
    プレビューの中を書き換えられるので、閲覧だけの共同開発者には渡さない。

    実行したことは必ず残す。あとから「誰が何をしたか」を辿れないまま
    コマンドを打てる口を開けてはいけない。
    """
    settings = enabled(request.app.state.settings)
    if not settings.preview_shell_enabled:
        # 画面から隠すだけでは止まらない。APIで断る。
        raise HTTPException(404, "この環境ではコマンドの実行を無効にしています。")
    identifier, tenant_id, user, _ = await run_in_threadpool(resolve, request, project_id)
    result = await backend(settings).execute(identifier, payload.command, tenant_id)
    await run_in_threadpool(audit, request, user.id, "preview.command", identifier,
                            payload.command[:500])
    return result
