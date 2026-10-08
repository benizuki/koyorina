import asyncio

from setup.preview import front


def request(path="/"):
    events = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        events.append(message)

    scope = {"type": "http", "method": "GET", "path": path,
             "query_string": b"", "headers": []}
    asyncio.run(front.app(scope, receive, send))
    return events


def test_root_is_served_without_requesting_the_api_backend(tmp_path, monkeypatch):
    (tmp_path / "index.html").write_text("<main>ready</main>")
    monkeypatch.setattr(front, "DIST", tmp_path)

    class NoUpstream:
        async def request(self, *args, **kwargs):
            raise AssertionError("GET / must not reach the backend")

    monkeypatch.setattr(front, "client", NoUpstream())
    events = request()
    assert events[0]["status"] == 200
    assert events[1]["body"] == b"<main>ready</main>"


def test_root_reports_building_instead_of_backend_404(tmp_path, monkeypatch):
    monkeypatch.setattr(front, "DIST", tmp_path)

    class NoUpstream:
        async def request(self, *args, **kwargs):
            raise AssertionError("a missing frontend must not become a backend 404")

    monkeypatch.setattr(front, "client", NoUpstream())
    events = request()
    assert events[0]["status"] == 503
    assert "画面を準備" in events[1]["body"].decode()


def headers_of(events):
    return {key.decode(): value.decode() for key, value in events[0]["headers"]}


def test_screens_and_assets_carry_the_same_security_headers_as_the_app(tmp_path, monkeypatch):
    """画面はアプリではなく前段が返す。アプリが付けるヘッダーが届かないので、ここで付ける。"""
    (tmp_path / "index.html").write_text("<main>ready</main>")
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "index.js").write_text("console.log(1)")
    monkeypatch.setattr(front, "DIST", tmp_path)
    for path in ("/", "/records/1", "/assets/index.js"):
        headers = headers_of(request(path))
        assert "script-src 'self'" in headers["content-security-policy"], path
        assert "'unsafe-inline'" not in headers["content-security-policy"].split("script-src")[1].split(";")[0]
        assert headers["x-content-type-options"] == "nosniff"
        assert headers["x-frame-options"] == "DENY"
        assert headers["referrer-policy"] == "strict-origin-when-cross-origin"


def test_extra_csp_origins_follow_the_app_settings(monkeypatch):
    import importlib
    monkeypatch.setenv("CSP_EXTRA_CONNECT_SRC", "https://api.example.test")
    reloaded = importlib.reload(front)
    try:
        assert "connect-src 'self' https://accounts.google.com https://api.example.test" in reloaded.CSP
    finally:
        monkeypatch.delenv("CSP_EXTRA_CONNECT_SRC")
        importlib.reload(front)


class Upstream:
    def __init__(self, status, headers):
        self.status, self.headers = status, headers

    async def request(self, *args, **kwargs):
        import httpx
        return httpx.Response(self.status, headers=self.headers, content=b'{"detail":"Not Found"}')


def test_the_app_keeps_its_own_headers_and_gets_only_the_missing_ones(tmp_path, monkeypatch):
    monkeypatch.setattr(front, "DIST", tmp_path)
    monkeypatch.setattr(front, "client", Upstream(200, {"content-type": "application/json",
        "content-security-policy": "default-src 'self'; connect-src 'self' https://api.example.test"}))
    headers = headers_of(request("/api/items"))
    # アプリが緩めたCSPは上書きしない。足りないものだけ足す。
    assert headers["content-security-policy"] == "default-src 'self'; connect-src 'self' https://api.example.test"
    assert headers["x-content-type-options"] == "nosniff"


def test_a_missing_api_is_reported_as_404_not_replaced_by_the_screen(tmp_path, monkeypatch):
    (tmp_path / "index.html").write_text("<main>ready</main>")
    monkeypatch.setattr(front, "DIST", tmp_path)
    monkeypatch.setattr(front, "client", Upstream(404, {"content-type": "application/json"}))
    events = request("/api/does-not-exist")
    assert events[0]["status"] == 404 and b"Not Found" in events[1]["body"]
    # 画面側のルート（APIではない）は、これまでどおり画面を返す。
    assert request("/some/screen")[0]["status"] == 200


def test_the_publication_front_is_the_same_as_the_preview_front():
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    assert (root / "setup/publication/front.py").read_text() == (root / "setup/preview/front.py").read_text()
