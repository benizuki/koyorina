"""生成アプリ側ホストでの本人確認。

Koyorina本体のセッションCookieは生成アプリのホストへ送られない（ホスト限定）。
そこで初回だけKoyorina本体へ寄って本人と権限を確かめ、短命の署名付きトークンを
持ってアプリ側ホストへ戻り、そのホスト専用のCookieを発行する（backend/api/app_handoff.py）。
権限そのものはCookieに頼らず、要求のたびにDBで確かめ直す（取り消しを即座に効かせる）。

ローカル検証（APP_ENV=local）は本人確認がCookieではなく接続元で決まるので、
引き渡しを挟まずに同じ判定（actor）を使う。
"""
from urllib.parse import urlencode
from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse
from backend.core.auth import actor
from backend.core.db import User
from backend.domain import app_hosts
from backend.domain.preview import base_path
from backend.domain.publication import published_base


def cookie_name(settings) -> str:
    # __Host- 付きは Domain 属性を持てない。兄弟の生成アプリから上書きされない。
    return "__Host-koyorina_app" if settings.app_origin.startswith("https://") else "koyorina_app"


def entrance(project_id, kind: str) -> str:
    return base_path(project_id) if kind == "preview" else published_base(project_id)


def origin_of(settings, project_id, kind: str) -> str:
    """そのアプリのオリジン。生成アプリへ APP_ORIGIN として渡す値でもある。"""
    return app_hosts.app_origin(settings.app_origin, project_id, kind, settings.apps_suffix)


def url_of(settings, project_id, kind: str) -> str:
    """画面に出すアプリの入口（絶対URL）。"""
    return origin_of(settings, project_id, kind) + entrance(project_id, kind)


def placement(request: Request):
    """この要求が届いたアプリ用ホスト。Koyorina本体へ来た要求なら None。"""
    settings = request.app.state.settings
    return app_hosts.parse_host(request.headers.get("host", ""), settings.apps_suffix)


def viewer(request: Request, db, project_id, kind: str) -> User:
    settings = request.app.state.settings
    if settings.app_env == "local":
        return actor(request, db)
    user_id = app_hosts.verify(settings.app_session_secret, "session",
                               request.cookies.get(cookie_name(settings), ""), project_id, kind,
                               app_hosts.SESSION_MAX_AGE)
    user = db.get(User, user_id) if user_id else None
    if user is None or not user.enabled:
        raise HTTPException(401, "ログインしてください。")
    return user


def navigation(request: Request) -> bool:
    """画面の遷移か。資産やAPIの取得にはリダイレクトせず401を返す（CORSで読めないため）。"""
    if request.method not in {"GET", "HEAD"}:
        return False
    mode = request.headers.get("sec-fetch-mode")
    if mode is not None:
        return mode == "navigate"
    return "text/html" in request.headers.get("accept", "")


def handoff_redirect(request: Request, project_id, kind: str) -> RedirectResponse:
    """本人確認のためにKoyorina本体へ送る。戻り先は今開こうとしたパス。"""
    settings = request.app.state.settings
    target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    query = urlencode({"project": str(project_id), "kind": kind, "next": target})
    return RedirectResponse(f"{settings.app_origin}/auth/app-handoff?{query}", status_code=303,
                            headers={"Cache-Control": "no-store"})


def relocate(request: Request, project_id, kind: str) -> RedirectResponse:
    """Koyorina本体のパスで開かれたアプリを、そのアプリ専用のホストへ送り直す。"""
    settings = request.app.state.settings
    target = origin_of(settings, project_id, kind) + request.url.path + (f"?{request.url.query}" if request.url.query else "")
    return RedirectResponse(target, status_code=307)
