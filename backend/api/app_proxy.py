"""生成アプリのプレビューを、アプリ専用のホスト（<id>-dev.<suffix>）で配信する。

Koyorina本体とは別オリジンなので、生成アプリの画面は閲覧者のKoyorinaセッションで
管理APIを呼べない。本人はアプリ側ホストのCookieで確かめ（core/app_session）、
開発権限は要求のたびに確かめ直す。Koyorinaのセッション、DB接続、Codex認証情報は転送しない。
"""
from uuid import UUID
import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from starlette.concurrency import run_in_threadpool
from backend.core.app_session import handoff_redirect, navigation, placement, relocate, viewer
from backend.domain.roles import can_manage, tenant_role
from backend.core.db import Project
from backend.api.projects import may_edit
from backend.core.preview_backend import backend
from backend.domain.preview import (base_path, forward_secret, identity_headers, request_headers,
                                    response_headers)

router = APIRouter(prefix="/apps")
MAX_BODY = 16 * 1024 * 1024
METHODS = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]


def notice(message: str, status: int = 503) -> Response:
    body = ("<!doctype html><meta charset=\"utf-8\"><title>アプリ</title>"
            "<style>body{font-family:system-ui;margin:3rem;color:#33474c}</style><p>"
            + message + "</p>")
    return Response(body, status_code=status, media_type="text/html; charset=utf-8",
                    headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


async def upstream(method: str, target: str, headers: dict, body: bytes):
    """転送はこの一箇所だけ。ループバックの固定ポート以外へは出さない。"""
    async with httpx.AsyncClient(timeout=60, trust_env=False, follow_redirects=False) as client:
        return await client.request(method, target, headers=headers, content=body or None)


def previewable(db, project_id, user):
    """プレビューを開けるのはオーナーと共同開発者だけ。"""
    project = db.get(Project, str(project_id))
    return project if project is not None and may_edit(db, project, user) else None


async def admit(request: Request, project_id):
    """届いたホストと本人を確かめる。応答を返すべきときは Response を返す。"""
    where = placement(request)
    if where is None:
        return None, relocate(request, project_id, "preview")
    if where != (project_id, "preview"):
        raise HTTPException(404, "アプリが見つかりません。")
    try:
        return await run_in_threadpool(authorize, request, project_id), None
    except HTTPException as exc:
        if exc.status_code == 401 and navigation(request):
            return None, handoff_redirect(request, project_id, "preview")
        raise


def authorize(request: Request, project_id):
    """画面の資産取得ごとに呼ばれる。行ロックを取らず、DBセッションを即座に返す。

    同期クエリのため、呼び出し側は必ず別スレッドで実行する。接続待ちが起きても
    イベントループを塞がないようにする。
    """
    with request.app.state.sessions() as db:
        user = viewer(request, db, project_id, "preview")
        project = previewable(db, project_id, user)

        if project is None:
            # 無関係な利用者には存在自体を知らせない。
            raise HTTPException(404, "アプリが見つかりません。")

        # 生成アプリにも役割を渡す。アプリ側で管理画面を出し分けられるようにする。
        # ロールはそのアプリが置かれたテナントでのもの（admin / developer / user）。
        identity = {"id": user.id, "email": user.email, "admin": can_manage(user),
                    "role": tenant_role(db, user, project.tenant_id) or "user",
                    "name": user.display_name}

    return project.id, identity


@router.api_route("/{project_id}", methods=METHODS, include_in_schema=False)
async def enter(project_id: UUID, request: Request):
    _, early = await admit(request, project_id)
    return early or RedirectResponse(base_path(project_id), status_code=307)


@router.api_route("/{project_id}/{path:path}", methods=METHODS, include_in_schema=False)
async def proxy(project_id: UUID, path: str, request: Request):
    admitted, early = await admit(request, project_id)
    if early:
        return early
    identifier, identity = admitted
    settings = request.app.state.settings

    if not settings.preview_enabled:
        return notice("このアプリの実行環境は無効です。管理者に設定を依頼してください。")
    base = backend(settings).target(identifier)

    if not base:
        return notice("アプリがまだ起動していません。プロジェクト画面から起動してください。", 409)

    length = request.headers.get("content-length")

    if length and (not length.isdigit() or int(length) > MAX_BODY):
        raise HTTPException(413, "送信データが大きすぎます。")

    body = await request.body()

    if len(body) > MAX_BODY:
        raise HTTPException(413, "送信データが大きすぎます。")

    headers = request_headers(request.headers.items(), identifier, request.app.state.session_cookie)
    headers.update(identity_headers(forward_secret(identifier, settings.app_session_secret), identity))
    target = f"{base}/{path}"

    if request.url.query:
        target += "?" + request.url.query
    try:
        response = await upstream(request.method, target, headers, body)
    except httpx.ConnectError:
        # 資産1件ごとにコンテナを問い合わせない。接続できないことをもって停止と扱う。
        return notice("アプリが起動していないか停止しています。プロジェクト画面から起動し直してください。", 409)
    except httpx.HTTPError:
        return notice("アプリが応答しません。起動状況とログを確認してください。")

    result = Response(response.content, status_code=response.status_code)

    # Set-Cookieが複数あるため、辞書化せずそのまま並べる。
    result.raw_headers = [(key.lower().encode("latin-1", "ignore"), value.encode("latin-1", "ignore"))
                          for key, value in response_headers(response.headers.multi_items(), identifier,
                                                                     settings.app_origin)]
    result.raw_headers.append((b"content-length", str(len(response.content)).encode()))

    return result
