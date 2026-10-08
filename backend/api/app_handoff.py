"""Koyorina本体のログインを、生成アプリ専用ホストへ引き渡す。

1. アプリ側ホストでCookieが無い → Koyorina本体の /auth/app-handoff へ（core/app_session）
2. 本体: 今のセッションで本人と権限を確かめ、60秒だけ有効な署名付きトークンを発行
3. アプリ側ホストの /__koyorina/handoff: トークンを確かめ、そのホスト専用のCookieを発行
4. 元のパスへ戻る。以降の権限確認はプロキシが要求ごとにDBで行う

本体のセッションCookieそのものは、アプリ側ホストへは一切渡らない。
"""
from urllib.parse import urlencode
from uuid import UUID
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from backend.api.app_proxy import notice, previewable
from backend.api.published_proxy import viewable
from backend.core.app_session import cookie_name, entrance, origin_of, placement
from backend.core.auth import actor
from backend.core.db import User
from backend.domain import app_hosts

router = APIRouter()
NO_STORE = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}


@router.get("/auth/app-handoff", include_in_schema=False)
def handoff(project: UUID, kind: str, request: Request, next: str = ""):
    settings = request.app.state.settings
    if kind not in app_hosts.KINDS or placement(request) is not None:
        raise HTTPException(404, "見つかりません。")
    with request.app.state.sessions() as db:
        try:
            user = actor(request, db)
        except HTTPException as exc:
            if exc.status_code != 401:
                raise
            return notice(f'<a href="{settings.app_origin}/" target="_top">Koyorinaにログイン</a>'
                          "してから、アプリを開き直してください。", 401)
        allowed = (previewable if kind == "preview" else viewable)(db, project, user)
        if allowed is None:
            return notice("アプリが見つかりません。", 404)
        user_id = user.id
    token = app_hosts.sign(settings.app_session_secret, "handoff", user_id, project, kind)
    origin = origin_of(settings, project, kind)
    query = urlencode({"token": token, "next": app_hosts.safe_next(next, entrance(project, kind))})
    return RedirectResponse(f"{origin}{app_hosts.HANDOFF_PATH}?{query}", status_code=303, headers=NO_STORE)


@router.get(app_hosts.HANDOFF_PATH, include_in_schema=False)
def receive(request: Request, token: str = "", next: str = ""):
    settings = request.app.state.settings
    where = placement(request)
    if where is None:
        raise HTTPException(404, "見つかりません。")
    project_id, kind = where
    user_id = app_hosts.verify(settings.app_session_secret, "handoff", token, project_id, kind,
                               app_hosts.HANDOFF_MAX_AGE)
    if user_id is None:
        return notice("ログインの引き継ぎに失敗しました。Koyorinaの画面からアプリを開き直してください。", 401)
    with request.app.state.sessions() as db:
        user = db.get(User, user_id)
        if user is None or not user.enabled:
            return notice("アプリが見つかりません。", 404)
    response = RedirectResponse(app_hosts.safe_next(next, entrance(project_id, kind)), status_code=303,
                                headers=NO_STORE)
    response.set_cookie(cookie_name(settings),
                        app_hosts.sign(settings.app_session_secret, "session", user_id, project_id, kind),
                        max_age=app_hosts.SESSION_MAX_AGE, path="/", httponly=True,
                        secure=settings.app_origin.startswith("https://"), samesite="lax")
    return response
