"""生成アプリを管理画面と別オリジンで配信する。閲覧者のKoyorina権限をアプリへ渡さない。"""
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4
import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from backend.core.db import (AppPublication, Base, Project, PublicationGrant, Tenant, User, UserTenant,
                             UserTenantRole)
from backend.domain import app_hosts
from backend.domain.preview import embeddable, request_cookies

PROJECT, OTHER, TENANT = str(uuid4()), str(uuid4()), str(uuid4())
KOYORINA = "https://koyorina.test"
APP = f"https://{PROJECT}.koyorina.test"


def test_hosts_are_one_level_below_koyorina_and_parse_back():
    suffix = app_hosts.host_suffix("https://koyorina.example.com")
    assert suffix == "koyorina.example.com"
    assert app_hosts.app_host(PROJECT, "preview", suffix) == f"{PROJECT}-dev.koyorina.example.com"
    assert app_hosts.app_host(PROJECT, "published", suffix) == f"{PROJECT}.koyorina.example.com"
    assert app_hosts.parse_host(f"{PROJECT}-dev.koyorina.example.com", suffix) == (UUID(PROJECT), "preview")
    assert app_hosts.parse_host(f"{PROJECT}.koyorina.example.com:443", suffix) == (UUID(PROJECT), "published")
    # 本体・別ドメイン・階層違い・IDでないものはアプリ用ホストではない。
    for host in ("koyorina.example.com", f"{PROJECT}.evil.example.com", f"x.{PROJECT}.koyorina.example.com",
                 "www.koyorina.example.com", f"{PROJECT}-prod.koyorina.example.com"):
        assert app_hosts.parse_host(host, suffix) is None
    # IPアドレスにはサブドメインを付けられない。ローカル検証は *.localhost へ寄せる。
    assert app_hosts.host_suffix("http://127.0.0.1:8080") == "localhost"
    assert app_hosts.app_origin("http://localhost:8080", PROJECT, "preview", "localhost") == \
        f"http://{PROJECT}-dev.localhost:8080"
    assert app_hosts.wildcard_source("https://koyorina.example.com", suffix) == "https://*.koyorina.example.com"


def test_tokens_are_bound_to_purpose_project_and_kind():
    token = app_hosts.sign("k" * 40, "handoff", "user-1", PROJECT, "preview")
    assert app_hosts.verify("k" * 40, "handoff", token, PROJECT, "preview", 60) == "user-1"
    assert app_hosts.verify("k" * 40, "handoff", token, OTHER, "preview", 60) is None
    assert app_hosts.verify("k" * 40, "handoff", token, PROJECT, "published", 60) is None
    assert app_hosts.verify("k" * 40, "session", token, PROJECT, "preview", 60) is None
    assert app_hosts.verify("x" * 40, "handoff", token, PROJECT, "preview", 60) is None
    assert app_hosts.verify("k" * 40, "handoff", token + "x", PROJECT, "preview", 60) is None


def test_return_path_stays_inside_the_app():
    base = f"/apps/{PROJECT}/"
    assert app_hosts.safe_next(base + "records?id=1", base) == base + "records?id=1"
    for unsafe in ("https://evil.example/", "//evil.example/", f"/apps/{OTHER}/", "/api/me",
                   base + "\\evil", base + "x\r\nSet-Cookie: a=b", ""):
        assert app_hosts.safe_next(unsafe, base) == base


def test_platform_cookies_are_not_forwarded_and_frames_are_limited_to_koyorina():
    forwarded = request_cookies("__Host-koyorina_app=t; koyorina_app=t; session=s; mine=1", PROJECT, "session")
    assert forwarded == "mine=1"
    assert embeddable("default-src 'self'; frame-ancestors 'self'", KOYORINA) == \
        f"default-src 'self'; frame-ancestors {KOYORINA}"


@pytest.fixture
def platform(monkeypatch):
    import backend.main
    from backend.api import published_proxy
    from backend.config.settings import Settings
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(backend.main, "database", lambda _: (engine, sessions))
    monkeypatch.setattr("backend.main.id_token.verify_oauth2_token",
                        lambda token, transport, audience: {"email": token, "email_verified": True,
                                                            "iss": "https://accounts.google.com"})
    settings = Settings(database_url="postgresql+psycopg://test@localhost/test", app_env="production",
                        app_origin=KOYORINA, app_session_secret="s" * 40, tenant_secret_key="e" * 40,
                        google_oauth_client_id="test-client")
    app = backend.main.create_app(settings)
    with sessions.begin() as db:
        viewer = User(email="viewer@example.test", role="admin")
        stranger = User(email="stranger@example.test", role="user")
        db.add_all([viewer, stranger, Tenant(id=TENANT, name="team")])
        db.flush()
        db.add_all([UserTenant(user_id=viewer.id, tenant_id=TENANT, role="user"),
                    UserTenantRole(user_id=viewer.id, tenant_id=TENANT, role="user"),
                    Project(id=PROJECT, owner_id=viewer.id, tenant_id=TENANT, name="App", purpose="test",
                            audience="team", fields=[]),
                    AppPublication(project_id=PROJECT, status="running")])
        db.flush()
        db.add(PublicationGrant(project_id=PROJECT, kind="user", subject_id=viewer.id))
        ids = {"viewer": viewer.id}

    async def call(*args):
        return {"status": "running"}
    monkeypatch.setattr(published_proxy, "call", call)
    sent = []

    class Upstream:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def request(self, method, url, **kwargs):
            sent.append((method, url, kwargs["headers"]))
            return httpx.Response(200, content=b"<html>app</html>", headers={"Content-Type": "text/html"})
    monkeypatch.setattr(published_proxy, "httpx", SimpleNamespace(AsyncClient=Upstream, HTTPError=httpx.HTTPError))
    client = TestClient(app, base_url=KOYORINA, headers={"Origin": KOYORINA})
    yield client, sessions, sent, ids
    engine.dispose()


def open_published_app(client):
    """利用者がKoyorinaから公開版を開いたときの往復を、リダイレクトごとに辿る。"""
    # アプリ専用ホストの直下で開く（IDをパスに重ねない）。
    page = f"{APP}/records?x=1"
    first = client.get(page, headers={"Sec-Fetch-Mode": "navigate"}, follow_redirects=False)
    assert first.status_code == 303
    handoff = urlsplit(first.headers["location"])
    assert f"{handoff.scheme}://{handoff.netloc}{handoff.path}" == f"{KOYORINA}/auth/app-handoff"
    second = client.get(first.headers["location"], follow_redirects=False)
    assert second.status_code == 303, second.text
    back = urlsplit(second.headers["location"])
    assert f"{back.scheme}://{back.netloc}{back.path}" == f"{APP}{app_hosts.HANDOFF_PATH}"
    assert parse_qs(back.query)["next"] == ["/records?x=1"]
    third = client.get(second.headers["location"], follow_redirects=False)
    assert third.status_code == 303
    assert third.headers["location"] == "/records?x=1"
    assert "__Host-koyorina_app=" in third.headers["set-cookie"]
    assert "HttpOnly" in third.headers["set-cookie"] and "Secure" in third.headers["set-cookie"]
    return client.get(page, headers={"Sec-Fetch-Mode": "navigate"}, follow_redirects=False)


def test_login_is_handed_to_the_app_host_without_the_koyorina_session(platform):
    client, sessions, sent, ids = platform
    assert client.post("/auth/google", json={"credential": "viewer@example.test"}).status_code == 200
    # 本番の本体セッションは __Host- 付き。サブドメインの生成アプリから上書きできない。
    assert "__Host-koyorina_session" in client.cookies
    page = open_published_app(client)
    assert page.status_code == 200 and page.text == "<html>app</html>"
    method, url, headers = sent[-1]
    assert url.endswith("/records?x=1")
    assert headers["X-Forge-User-Email"] == "viewer@example.test"
    # 本体のセッションCookieはアプリ側ホストへ届かず、アプリ側の本人確認Cookieも転送しない。
    assert "Cookie" not in headers
    assert page.headers["content-security-policy"] == f"frame-ancestors {KOYORINA}"
    # 本体のHSTSはサブドメインに及ばない。アプリ用ホストにも付ける。
    assert page.headers["strict-transport-security"] == "max-age=31536000"
    assert page.headers["x-content-type-options"] == "nosniff"
    # 資産やAPIの取得はリダイレクトしない（別オリジンへの転送はCORSで読めない）。
    client.cookies.clear(domain=f"{PROJECT}.koyorina.test")
    assert client.get(f"{APP}/published-apps/{PROJECT}/api/items",
                      headers={"Sec-Fetch-Mode": "cors"}).status_code == 401


def test_permissions_are_rechecked_on_every_request(platform):
    client, sessions, sent, ids = platform
    client.post("/auth/google", json={"credential": "viewer@example.test"})
    assert open_published_app(client).status_code == 200
    with sessions.begin() as db:
        db.query(PublicationGrant).delete()
    assert client.get(f"{APP}/published-apps/{PROJECT}/").status_code == 404


def test_app_hosts_cannot_reach_the_management_api(platform):
    client, sessions, sent, ids = platform
    client.post("/auth/google", json={"credential": "viewer@example.test"})
    open_published_app(client)
    # アプリ側ホストのパスは、どれも生成アプリへ渡る。本体の管理API・ログイン・画面には届かない。
    for path in ("/api/me", "/api/projects", "/auth/app-handoff", "/", "/assets/app.js"):
        response = client.get(APP + path, follow_redirects=False)
        assert response.status_code == 200 and response.text == "<html>app</html>", path
        assert sent[-1][1].endswith(path), path
    login = client.post(APP + "/auth/google", json={"credential": "viewer@example.test"}, headers={"Origin": APP})
    assert sent[-1][0] == "POST" and sent[-1][1].endswith("/auth/google")
    assert "koyorina_session" not in login.headers.get("set-cookie", "")
    # 生成アプリの画面から本体の管理APIへ送っても、Originが違うので処理されない
    # （同一サイトなのでLaxのCookieは付くが、変更系はOrigin照合で止まる）。
    forged = client.post(f"{KOYORINA}/api/projects", json={}, headers={"Origin": APP})
    assert forged.status_code == 403


def test_handoff_refuses_strangers_and_foreign_tokens(platform):
    client, sessions, sent, ids = platform
    client.post("/auth/google", json={"credential": "stranger@example.test"})
    refused = client.get(f"/auth/app-handoff?project={PROJECT}&kind=published", follow_redirects=False)
    assert refused.status_code == 404
    # 別のアプリ宛てのトークンは、このアプリのホストでは使えない。
    token = app_hosts.sign("s" * 40, "handoff", ids["viewer"], OTHER, "published")
    foreign = client.get(f"{APP}{app_hosts.HANDOFF_PATH}?token={token}", follow_redirects=False)
    assert foreign.status_code == 401 and "set-cookie" not in foreign.headers
    # 未ログインならログインを促すだけで、トークンは出さない。
    client.post("/auth/logout", json={})
    anonymous = client.get(f"/auth/app-handoff?project={PROJECT}&kind=published", follow_redirects=False)
    assert anonymous.status_code == 401 and "location" not in anonymous.headers


def test_koyorina_paths_move_to_the_app_host_and_frames_are_allowed(platform):
    client, sessions, sent, ids = platform
    moved = client.get(f"/published-apps/{PROJECT}/a?b=1", follow_redirects=False)
    # 本体のホストで開かれた以前のURLは、アプリ専用ホストの直下へ送る。
    assert moved.status_code == 307 and moved.headers["location"] == f"{APP}/a?b=1"
    policy = client.get("/api/config").headers["content-security-policy"]
    assert "frame-src 'self' https://*.koyorina.test " in policy


def test_images_built_for_the_old_url_keep_working(platform):
    """以前の形でビルドした画面は、資産を /published-apps/<id>/… で読みに来る。そのまま通す。"""
    client, sessions, sent, ids = platform
    client.post("/auth/google", json={"credential": "viewer@example.test"})
    open_published_app(client)
    legacy = client.get(f"{APP}/published-apps/{PROJECT}/assets/app.js")
    assert legacy.status_code == 200 and sent[-1][1].endswith("/assets/app.js")
    # 別のアプリのIDを含むパスは、このアプリのパスとして生成アプリへ渡るだけ（他のアプリには届かない）。
    client.get(f"{APP}/published-apps/{OTHER}/x")
    assert sent[-1][1].endswith(f"/published-apps/{OTHER}/x")


def test_cookies_and_redirects_follow_the_shape_of_the_request(platform, monkeypatch):
    from backend.api import published_proxy
    client, sessions, sent, ids = platform
    client.post("/auth/google", json={"credential": "viewer@example.test"})
    open_published_app(client)

    class Upstream:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def request(self, method, url, **kwargs):
            return httpx.Response(302, headers={"Location": "/login", "Set-Cookie": "sid=v; Path=/; HttpOnly"})
    monkeypatch.setattr(published_proxy, "httpx", SimpleNamespace(AsyncClient=Upstream, HTTPError=httpx.HTTPError))
    root = client.get(f"{APP}/records", follow_redirects=False)
    assert root.headers["location"] == "/login" and "Path=/;" in root.headers["set-cookie"] + ";"
    legacy = client.get(f"{APP}/published-apps/{PROJECT}/records", follow_redirects=False)
    assert legacy.headers["location"] == f"/published-apps/{PROJECT}/login"
    assert f"Path=/published-apps/{PROJECT}/" in legacy.headers["set-cookie"]
