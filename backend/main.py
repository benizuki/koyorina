from contextlib import asynccontextmanager
import asyncio
from pathlib import Path
import hashlib
import secrets
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2 import id_token
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from urllib.parse import urlparse
from backend.core.request_log import configure_logging
from backend.api.projects import router as project_router
from backend.api.pdf_fields import router as pdf_router
from backend.api.sample_data import router as sample_data_router
from backend.api.system_settings import router as system_settings_router
from backend.api.tenant_llm import router as tenant_llm_router
from backend.api.masters import router as masters_router
from backend.api.voice import router as voice_router
from backend.api.codex import router as codex_router
from backend.api.generation import router as generation_router
from backend.api.preview import router as preview_router
from backend.api.app_proxy import router as app_proxy_router
from backend.api.publication import router as publication_router, reconcile_publications
from backend.api.published_proxy import router as published_proxy_router
from backend.api.app_handoff import router as app_handoff_router
from backend.api.support import router as support_router
from backend.core import gemini_client
from backend.core.gemini_client import available as gemini_available
from backend.domain import app_hosts, system_gemini, system_llm, tenant_ai
from backend.domain.preview import base_path
from backend.domain.publication import published_base
from backend.api.support import expire_sessions
from backend.api.tenant_migrations import router as tenant_migration_router
from backend.api.tenant_migrations import cleanup_retained_sources
from backend.api.storage_usage import router as storage_usage_router
from backend.config.settings import Settings
from backend.core.auth import actor, get_db
from backend.core.db import Audit, SystemSetting, User, UserTenant, database
from backend.domain.roles import (admin_tenant_ids, can_develop_somewhere, can_manage,
                                  developer_tenant_ids, operator_tenant_ids, tenant_roles, tenant_role_sets)


def create_app(settings: Settings | None = None):
    # koyorina.* のINFOを出す。API呼び出しはuvicornのアクセスログが出している。
    configure_logging()
    settings = settings or Settings()
    engine, sessions = database(settings.database_url)

    def system_llm_values():
        """画面（システム設定）で指定した生成AIの設定。読めなければ環境の設定のまま。"""
        with sessions() as db:
            rows = {row.key: row.value for row in db.scalars(select(SystemSetting).where(
                SystemSetting.key.in_((system_gemini.KEY, *system_llm.KEYS))))}
        return {"gemini": rows.get(system_gemini.KEY),
                **{key: rows.get(key) for key in system_llm.KEYS}}
    gemini_client.use_system_settings(system_llm_values)

    @asynccontextmanager
    async def lifespan(app):
        async def expire_support_loop():
            while True:
                await asyncio.sleep(60)
                await expire_sessions(app)
                await cleanup_retained_sources(app)
        task = asyncio.create_task(expire_support_loop())
        publication_task = asyncio.create_task(reconcile_publications(app))
        try:
            yield
        finally:
            task.cancel()
            publication_task.cancel()
            try:
                await publication_task
            except asyncio.CancelledError:
                pass
            try:
                await task
            except asyncio.CancelledError:
                pass
            engine.dispose()

    app = FastAPI(title="Koyorina", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings, app.state.sessions = settings, sessions
    app.state.pdf_extraction_lock = asyncio.Lock()
    app.state.purpose_draft_lock = asyncio.Lock()
    app.state.voice_lock = asyncio.Lock()
    app.state.preview_lock = asyncio.Lock()
    # 生成アプリは兄弟のサブドメイン（domain/app_hosts）で動く。サブドメインのJavaScriptは
    # 親ドメイン向けのCookieを書けるので、本番は __Host- 付きにして上書きさせない。
    app.state.session_cookie = "__Host-koyorina_session" if settings.app_origin.startswith("https://") else "session"
    app.add_middleware(SessionMiddleware, secret_key=settings.app_session_secret or secrets.token_urlsafe(48),
                       session_cookie=app.state.session_cookie,
                       https_only=settings.app_env == "production", same_site="lax", max_age=28800)
    hosts = [urlparse(settings.app_origin).hostname, f"*.{settings.apps_suffix}"]
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=hosts + ["testserver"] if settings.app_env == "local" else hosts)

    if settings.app_env == "local":
        # ローカル検証では 127.0.0.1 / [::1] で開かれることがある。Cookie とOriginの検査は
        # APP_ORIGIN のホストに揃えてあるので、受け入れずに同じパスの APP_ORIGIN へ送る。
        # 後から足したミドルウェアほど外側で動くので、ホストの検査より先に効く。
        canonical = urlparse(settings.app_origin)

        @app.middleware("http")
        async def loopback_alias(request, call_next):
            host = urlparse(f"//{request.headers.get('host', '')}").hostname
            if host in {"127.0.0.1", "::1"} and host != canonical.hostname:
                target = f"{settings.app_origin}{request.url.path}"
                if request.url.query:
                    target += "?" + request.url.query
                return RedirectResponse(target, status_code=307)
            return await call_next(request)

    # プレビューは管理画面のiframeに別オリジンとして載る。
    app_frames = app_hosts.wildcard_source(settings.app_origin, settings.apps_suffix)

    @app.middleware("http")
    async def security(request, call_next):
        where = app_hosts.parse_host(request.headers.get("host", ""), settings.apps_suffix)
        if where is not None:
            # 生成アプリ専用のホスト。届くのはそのアプリのパスと引き渡しの口だけで、
            # 管理API・ログイン・管理画面は返さない。応答はプロキシ側で整える。
            project_id, kind = where
            base = base_path(project_id) if kind == "preview" else published_base(project_id)
            path = request.url.path
            if path == "/":
                return RedirectResponse(base, status_code=307)
            if path == app_hosts.HANDOFF_PATH or path == base.rstrip("/") or path.startswith(base):
                response = await call_next(request)
                # 生成アプリが付けたHSTSはプロキシで落としている（アプリに決めさせない）。
                # 本体のHSTSはサブドメインに及ばないので、アプリ用ホストにはここで付ける。
                if settings.app_env == "production":
                    response.headers["Strict-Transport-Security"] = "max-age=31536000"
                response.headers.setdefault("X-Content-Type-Options", "nosniff")
                return response
            return JSONResponse({"error": "見つかりません。", "status_code": 404}, status_code=404,
                                headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"})
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            expected_content_type = ("application/pdf" if request.url.path == "/api/pdf-fields"
                                     else "audio/wav" if request.url.path == "/api/voice"
                                     # 添付は画像・PDF・テキストが来る。中身で種類を確かめるので、
                                     # ここは1つの形式に固定し、宣言そのものは信用しない。
                                     else "application/octet-stream"
                                     if request.url.path.endswith("/attachments")
                                     else "application/zip" if request.url.path.endswith("/local-artifact")
                                     else "application/json")
            if request.headers.get("origin") != settings.app_origin:
                response = JSONResponse({"error": "元の画面から操作をやり直してください。", "status_code": 403}, status_code=403)
            elif request.headers.get("content-type", "").split(";")[0] != expected_content_type:
                response = JSONResponse({"error": "送信形式を確認してください。", "status_code": 415}, status_code=415)
            else:
                response = await call_next(request)
        else:
            response = await call_next(request)
        response.headers.update({
            "Content-Security-Policy": f"default-src 'self'; script-src 'self' https://accounts.google.com/gsi/client; style-src 'self' 'unsafe-inline' https://accounts.google.com/gsi/style; img-src 'self' data: blob:; font-src 'self'; connect-src 'self' https://accounts.google.com/gsi/; frame-src 'self' {app_frames} https://accounts.google.com/gsi/; object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
            "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY",
            "Referrer-Policy": "no-referrer", "Permissions-Policy": "camera=(), microphone=(self), geolocation=()",
            "Cache-Control": "no-store",
        })
        if settings.app_env == "production":
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        return response

    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        return JSONResponse({"error": exc.detail, "status_code": exc.status_code}, status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        # 入力値そのものは返さない。こちらが書いた日本語の理由（TenantAiError）だけは、
        # 何を直せばよいかが分かるようにそのまま返す。
        reasons = [str(error["ctx"]["error"]) for error in exc.errors()
                   if isinstance((error.get("ctx") or {}).get("error"), tenant_ai.TenantAiError)]
        message = reasons[0] if reasons else "入力内容を確認してください。必須項目・文字数・項目名の重複を見直してください。"
        return JSONResponse({"error": message, "status_code": 422}, status_code=422)

    @app.exception_handler(Exception)
    async def unexpected_error(request, exc):
        # DB接続文字列や認証情報を利用者・ログへ漏らさない。
        return JSONResponse({"error": "処理できませんでした。管理者に接続設定を確認してください。", "status_code": 500}, status_code=500,
                            headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"})

    @app.get("/healthz")
    def health():
        return {"status": "ok"}

    @app.get("/api/config")
    def config():
        # Antigravity と OpenAI 互換 API は、システム設定で有効・無効を変えられる。
        current = gemini_client.effective(settings)
        return {"auth_mode": "dev-bypass" if settings.app_env == "local" else "google",
                "google_client_id": settings.google_oauth_client_id, "generation_ready": bool(settings.codex_controller_url),
                "gemini_available": bool(settings.codex_controller_url and gemini_available(settings)),
                "antigravity_available": bool(settings.codex_controller_url and current.antigravity_enabled),
                "codex_available": settings.codex_enabled,
                "openai_compatible_available": bool(settings.codex_controller_url and current.openai_compatible_enabled),
                "openai_compatible_label": current.openai_compatible_label,
                "claude_available": bool(settings.codex_controller_url and current.claude_enabled),
                "pdf_extraction_enabled": settings.pdf_extraction_enabled,
                "preview_shell_enabled": settings.preview_enabled and settings.preview_shell_enabled,
                "local_codex_enabled": settings.local_codex_enabled,
                "preview_enabled": settings.preview_enabled}

    @app.get("/api/me")
    def me(request: Request, db: Session = Depends(get_db)):
        user = actor(request, db)
        return {"id": user.id, "email": user.email, "display_name": user.display_name,
                "role": user.role, "can_manage_users": can_manage(user),
                # どこかのテナントでアプリを作れるか。どこで作れるかは develop_tenant_ids。
                "can_develop": can_develop_somewhere(db, user),
                "can_use_codex": settings.codex_enabled and user.codex_enabled,
                "department_id": user.department_id,
                # アプリを置けるテナント。画面は選べるものだけを出す。
                "tenant_ids": sorted(db.scalars(select(UserTenant.tenant_id)
                                                .where(UserTenant.user_id == user.id))),
                # アプリを作れるテナント（developer ロール）。
                "develop_tenant_ids": sorted(developer_tenant_ids(db, user)),
                # 旧クライアント向けの代表ロールと、独立したロール集合。
                "tenant_roles": tenant_roles(db, user),
                "tenant_role_sets": tenant_role_sets(db, user),
                "operator_tenant_ids": sorted(operator_tenant_ids(db, user)),
                # 生成AIの設定と利用状況を扱えるテナント（システム管理者は全テナント）。
                "admin_tenant_ids": sorted(admin_tenant_ids(db, user)),
                "auth_mode": "dev-bypass" if settings.app_env == "local" else "google"}

    class Credential(BaseModel):
        credential: str = Field(min_length=1, max_length=16384)

    @app.post("/auth/google")
    def google_login(payload: Credential, request: Request, db: Session = Depends(get_db)):
        if not settings.google_oauth_client_id:
            raise HTTPException(503, "Googleログインの設定を管理者に依頼してください。")
        try:
            info = id_token.verify_oauth2_token(payload.credential, GoogleRequest(), settings.google_oauth_client_id)
            if info.get("iss") not in {"accounts.google.com", "https://accounts.google.com"} or info.get("email_verified") is not True:
                raise ValueError("invalid identity")
        except Exception:
            # 不正トークンをログへ出さない。
            db.add(Audit(action="login.failed"))
            db.commit()
            raise HTTPException(401, "Googleログインをやり直してください。") from None
        user = db.scalar(select(User).where(User.email == str(info.get("email", "")).lower(), User.enabled.is_(True)))
        if user is None:
            db.add(Audit(action="login.denied"))
            db.commit()
            raise HTTPException(403, "招待されていないか、利用が停止されています。")
        subject = str(info.get("sub", ""))[:64]
        if user.google_subject and subject != user.google_subject:
            # 登録済みのGoogleアカウントと一致しない。メールアドレスの使い回しを弾く。
            db.add(Audit(actor_id=user.id, action="login.subject_mismatch"))
            db.commit()
            raise HTTPException(403, "登録済みのGoogleアカウントと一致しません。管理者に確認してください。")
        if not user.google_subject and subject:
            user.google_subject = subject  # 初回ログインで控える。次回からはこれと突き合わせる。
        request.session.clear()
        request.session.update({"user_id": user.id, "email": user.email, "client": hashlib.sha256(settings.google_oauth_client_id.encode()).hexdigest()})
        db.add(Audit(actor_id=user.id, action="login.succeeded"))
        db.commit()
        return {"ok": True}

    @app.post("/auth/logout")
    def logout(request: Request):
        request.session.clear()
        return {"ok": True}


    app.include_router(project_router)
    app.include_router(codex_router)
    app.include_router(generation_router)
    app.include_router(preview_router)
    app.include_router(app_proxy_router)
    app.include_router(publication_router)
    app.include_router(published_proxy_router)
    app.include_router(app_handoff_router)
    app.include_router(pdf_router)
    app.include_router(sample_data_router)
    app.include_router(voice_router)
    app.include_router(masters_router)
    app.include_router(tenant_llm_router)
    app.include_router(system_settings_router)
    app.include_router(support_router)
    app.include_router(tenant_migration_router)
    app.include_router(storage_usage_router)
    dist = Path(__file__).resolve().parent.parent / "frontend" / "dist"

    @app.get("/{path:path}")
    def spa(path: str):
        if path.startswith(("api", "auth", "healthz")):
            raise HTTPException(404, "見つかりません。")
        target = (dist / path).resolve()
        if not target.is_relative_to(dist):
            raise HTTPException(404, "見つかりません。")
        if path and target.is_file():
            return FileResponse(target)
        if not (dist / "index.html").is_file():
            raise HTTPException(503, "画面をビルドしてください。")
        return FileResponse(dist / "index.html")

    return app
