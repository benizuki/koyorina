"""Published applications: authorize every request, isolate preview cookies and identity."""
from uuid import UUID
import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from starlette.concurrency import run_in_threadpool
from backend.core.auth import actor
from backend.core.db import AppPublication, Project
from backend.core.publication_client import call
from backend.domain.roles import can_manage
from backend.domain.publication import published_secret, permitted, published_base
from backend.domain.preview import request_headers, response_headers, identity_headers, forward_secret, base_path
from backend.api.app_proxy import MAX_BODY, METHODS, notice

router = APIRouter(prefix='/published-apps')


def authorize(request, project_id):
    with request.app.state.sessions() as db:
        user = actor(request, db)
        project = db.get(Project, str(project_id))
        pub = db.get(AppPublication, str(project_id))

        if not project or not pub or not permitted(db, project, user, pub):
            raise HTTPException(404, 'アプリが見つかりません。')

        return {'id': user.id, 'email': user.email, 'admin': can_manage(user)}, pub.status


@router.api_route('/{project_id}', methods=METHODS, include_in_schema=False)
async def enter(project_id: UUID, request: Request):
    await run_in_threadpool(authorize, request, project_id)
    return RedirectResponse(published_base(project_id), status_code=307)


@router.api_route('/{project_id}/{path:path}', methods=METHODS, include_in_schema=False)
async def proxy(project_id: UUID, path: str, request: Request):
    identity, status = await run_in_threadpool(authorize, request, project_id)

    if status != 'running':
        return notice('このアプリは公開を停止しているか、更新中です。', 503)
    state = await call(request.app.state.settings, 'GET', f'/projects/{project_id}')

    if not state or state['status'] != 'running':
        return notice('このアプリは起動していないか、更新中です。', 503)

    # Never accept an upstream URL from a response or browser input.
    settings = request.app.state.settings

    if (getattr(settings, 'app_env', None) == 'local'
            and getattr(settings, 'publication_controller_url', None) == 'http://publication-controller:8080'):
        target = f'http://koyorina-published-{project_id}:8080'
    else:
        target = f'http://published-{project_id}.{settings.app_name}-published.svc:8080'

    length = request.headers.get('content-length')

    if length and (not length.isdigit() or int(length) > MAX_BODY):
        raise HTTPException(413, '送信データが大きすぎます。')

    body = bytearray()

    async for chunk in request.stream():
        body.extend(chunk)

        if len(body) > MAX_BODY:
            raise HTTPException(413, '送信データが大きすぎます。')

    # Cookie names are separate from previews; rewrite only the generated app's cookie path.
    cookie_id = UUID(int=project_id.int ^ (1 << 127))

    headers = request_headers(request.headers.items(), cookie_id, request.app.state.session_cookie)
    headers.update(identity_headers(published_secret(project_id,
        request.app.state.settings.app_session_secret), identity))
    target += '/' + path

    if request.url.query:
        target += '?' + request.url.query
    try:
        async with httpx.AsyncClient(timeout=60, trust_env=False, follow_redirects=False) as client:
            upstream = await client.request(request.method, target, headers=headers, content=bytes(body) or None)
    except httpx.HTTPError:
        return notice('アプリが応答しません。管理者に確認してください。', 503)

    result = Response(upstream.content, status_code=upstream.status_code)
    rewritten = []

    for key, value in upstream.headers.multi_items():
        if key.lower() == 'location' and value.startswith(published_base(project_id)):
            rewritten.append((key, value))
        else:
            rewritten.extend(response_headers([(key, value)], cookie_id))

    result.raw_headers = [(k.lower().encode('latin-1'),
        v.replace(base_path(cookie_id), published_base(project_id)).encode('latin-1')) for k, v in rewritten]

    result.raw_headers.append((b'cache-control', b'no-store'))
    result.raw_headers.append((b'content-length', str(len(upstream.content)).encode()))

    return result
