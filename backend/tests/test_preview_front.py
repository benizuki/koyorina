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
