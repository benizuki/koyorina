"""プレビューの前段。ビルド済み画面を配信し、それ以外を生成アプリへ転送する。

生成アプリが画面を配信するとは限らないため、配信はここで必ず行う。
アプリ本体は 127.0.0.1:$PREVIEW_APP_PORT で動かし、外へは出さない。
"""
import asyncio
import mimetypes
import os
from pathlib import Path
import httpx

DIST = Path(os.environ.get("FRONTEND_DIST", "/workspace/frontend/dist"))
UPSTREAM = "http://127.0.0.1:" + os.environ.get("PREVIEW_APP_PORT", "8081")
HEALTH = "/__preview/health"
HOP_BY_HOP = {b"connection", b"keep-alive", b"transfer-encoding", b"upgrade", b"content-encoding",
              b"content-length", b"proxy-authenticate", b"proxy-authorization", b"te", b"trailer"}
client = httpx.AsyncClient(timeout=60, trust_env=False, follow_redirects=False)


def static_file(path: str):
    """distの中だけを返す。シンボリックリンクや親参照でその外へ出さない。"""
    root = DIST.resolve()
    candidate = (root / path.lstrip("/")).resolve()
    if not candidate.is_relative_to(root) or not candidate.is_file() or candidate.is_symlink():
        return None
    return candidate


async def send(send_fn, status, headers, body: bytes):
    await send_fn({"type": "http.response.start", "status": status, "headers": headers})
    await send_fn({"type": "http.response.body", "body": body})


async def app(scope, receive, send_fn):
    if scope["type"] != "http":
        return
    path = scope["path"]
    if path == HEALTH:
        # HTTPの / を叩くと、API専用バックエンドでは正常でも404がアクセスログへ残る。
        # TCP接続だけで待受開始を確認し、利用者向けログに偽のエラーを出さない。
        try:
            host, port = UPSTREAM.removeprefix("http://").rsplit(":", 1)
            reader, writer = await asyncio.wait_for(asyncio.open_connection(host, int(port)), timeout=3)
            writer.close()
            await writer.wait_closed()
        except (OSError, ValueError, asyncio.TimeoutError):
            return await send(send_fn, 503, [(b"content-type", b"application/json")],
                              b'{"status":"starting"}')
        if not (DIST / "index.html").is_file():
            return await send(send_fn, 503, [(b"content-type", b"application/json")],
                              b'{"status":"building"}')
        return await send(send_fn, 200, [(b"content-type", b"application/json")], b'{"status":"ok"}')

    if scope["method"] in {"GET", "HEAD"}:
        target = static_file(path)
        if target is not None:
            kind = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            return await send(send_fn, 200, [(b"content-type", kind.encode()),
                                             (b"cache-control", b"no-store")], target.read_bytes())
        index = DIST / "index.html"
        frontend_path = not path.lstrip("/").startswith(("api/", "auth/", "login", "logout", "healthz"))
        # 画面の入口とクライアント側ルートは前段で直接返す。API専用バックエンドへ
        # GET / を投げないため、正常な構成で404アクセスログが出ない。
        if index.is_file() and frontend_path:
            return await send(send_fn, 200, [(b"content-type", b"text/html; charset=utf-8"),
                                             (b"cache-control", b"no-store")], index.read_bytes())
        if not index.is_file() and frontend_path:
            return await send(send_fn, 503, [(b"content-type", b"text/plain; charset=utf-8"),
                                             (b"cache-control", b"no-store")],
                              "画面を準備しています。ログでビルド状況を確認してください。".encode())

    body = b""
    while True:
        message = await receive()
        body += message.get("body", b"")
        if not message.get("more_body"):
            break
    headers = [(key, value) for key, value in scope["headers"] if key.lower() not in HOP_BY_HOP]
    url = UPSTREAM + path + ("?" + scope["query_string"].decode() if scope["query_string"] else "")
    try:
        upstream = await client.request(scope["method"], url, headers=headers, content=body or None)
    except httpx.HTTPError:
        return await send(send_fn, 502, [(b"content-type", b"text/plain; charset=utf-8")],
                          "アプリが応答しません。ログを確認してください。".encode())

    index = DIST / "index.html"
    if upstream.status_code == 404 and scope["method"] == "GET" and index.is_file():
        # 画面側のルーティングはアプリの経路に無い。SPAとして扱う。
        return await send(send_fn, 200, [(b"content-type", b"text/html; charset=utf-8"),
                                         (b"cache-control", b"no-store")], index.read_bytes())
    out = [(key.encode("latin-1", "ignore"), value.encode("latin-1", "ignore"))
           for key, value in upstream.headers.multi_items() if key.lower().encode() not in HOP_BY_HOP]
    return await send(send_fn, upstream.status_code, out, upstream.content)
