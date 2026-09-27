"""セキュリティヘッダーが実際に付いていることを確認する。

このテストがある意味は「CSP を緩めたことに気付けるようにする」こと。
外部サービスを足すときに、とりあえず 'unsafe-inline' や * を入れて
動かしてしまい、そのまま本番に出る、という事故を止める。
"""
from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from core.security_headers import SecurityHeadersMiddleware, build_content_security_policy


@pytest.fixture
def client() -> TestClient:
    async def api(_request):
        return JSONResponse({"ok": True})

    async def page(_request):
        return HTMLResponse("<!doctype html><title>t</title>")

    app = Starlette(routes=[Route("/api/items", api), Route("/", page)])
    app.add_middleware(SecurityHeadersMiddleware)
    return TestClient(app)


def test_security_headers_are_present(client: TestClient) -> None:
    headers = client.get("/api/items").headers
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["X-Frame-Options"] == "DENY"
    assert headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
    assert "Content-Security-Policy" in headers


def test_api_responses_are_not_cached(client: TestClient) -> None:
    # 権限を変えた直後に古い応答が返ると、原因が分からない不具合になる。
    assert client.get("/api/items").headers["Cache-Control"] == "no-store, max-age=0"


def test_html_is_revalidated(client: TestClient) -> None:
    # index.html がキャッシュされると、デプロイしても古い SPA が出続ける。
    assert "must-revalidate" in client.get("/").headers["Cache-Control"]


def test_hsts_only_on_https(client: TestClient) -> None:
    # http の開発環境で HSTS を付けると、そのホストが https に固定され
    # localhost が開けなくなる。
    assert "Strict-Transport-Security" not in client.get("/api/items").headers

    forwarded = client.get("/api/items", headers={"X-Forwarded-Proto": "https"})
    assert forwarded.headers["Strict-Transport-Security"] == "max-age=31536000"


def test_script_src_does_not_allow_inline() -> None:
    # script-src に 'unsafe-inline' が入ると、CSP による XSS の緩和が
    # ほぼ意味を失う。style-src の 'unsafe-inline' は Vuetify のために許容する。
    policy = build_content_security_policy()
    script_src = next(d for d in policy.split("; ") if d.startswith("script-src "))
    assert "'unsafe-inline'" not in script_src
    assert "'unsafe-eval'" not in script_src
    assert "*" not in script_src
