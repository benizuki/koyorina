"""実PostgreSQLを使用する。テストごとに外側トランザクションをrollback。"""
import os
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker
from backend.config.settings import Settings
from backend.core.db import Audit, User, database
from backend.main import create_app
from backend.tests.test_projects import INPUT


# 接続先は環境変数で明示する。設定ファイル（.env）から拾うと、何に繋がるかが見えない。
# 例: KOYORINA_TEST_DATABASE_URL=postgresql+psycopg://forge:<pw>@127.0.0.1:55432/forge
TEST_DATABASE_URL = os.environ.get("KOYORINA_TEST_DATABASE_URL", "")


@pytest.fixture
def context(monkeypatch):
    if not TEST_DATABASE_URL:
        pytest.skip("KOYORINA_TEST_DATABASE_URL（移行済みの専用PostgreSQL）が未指定です。")
    from sqlalchemy.engine import make_url
    assert make_url(TEST_DATABASE_URL).host in {"127.0.0.1", "localhost"}, "専用ローカルDBが必要です。"
    settings = Settings(_env_file=None, app_env="production", database_url=TEST_DATABASE_URL,
                        app_origin="https://forge.test", app_session_secret="test-only-session-key-32-characters-long",
                        google_oauth_client_id="test-client")
    engine, _ = database(settings.database_url)
    connection = engine.connect()
    transaction = connection.begin()
    sessions = sessionmaker(bind=connection, join_transaction_mode="create_savepoint", expire_on_commit=False)
    with sessions.begin() as db:
        db.add_all([User(email="alice@example.com", role="admin"), User(email="bob@example.com", role="member"), User(email="disabled@example.com", enabled=False)])
    app = create_app(settings)
    app.state.sessions = sessions
    def verify(token, transport, audience):
        assert audience == "test-client"
        if token == "invalid":
            raise ValueError("invalid")
        return {"email": token, "email_verified": True, "iss": "https://accounts.google.com"}
    monkeypatch.setattr("backend.main.id_token.verify_oauth2_token", verify)
    with TestClient(app, base_url="https://forge.test", headers={"Origin": "https://forge.test"}) as client:
        yield client, sessions
    transaction.rollback()
    connection.close()
    engine.dispose()


def login(client, email="alice@example.com"):
    return client.post("/auth/google", json={"credential": email})


def test_auth_required_and_invalid_logins(context):
    client, _ = context
    assert client.get("/api/projects").status_code == 401
    assert login(client, "invalid").status_code == 401
    assert login(client, "outsider@example.com").status_code == 403
    assert login(client, "disabled@example.com").status_code == 403


def test_save_approve_revise_and_audit(context):
    client, sessions = context
    assert login(client).status_code == 200
    response = client.post("/api/projects", json=INPUT)
    assert response.status_code == 201, response.text
    p = response.json()
    path = f"/api/projects/{p['id']}"
    assert client.post(path + "/generate", json={}).status_code == 409
    assert client.post(path + "/approve", json={"revision": 1}).json()["status"] == "approved"
    assert client.post(path + "/generate", json={}).status_code == 503
    updated = client.put(path, json=INPUT | {"name": "備品台帳", "revision": 1}).json()
    assert updated["revision"] == 2 and updated["approved_revision"] is None
    assert updated["status"] == "draft"
    assert client.post(path + "/approve", json={"revision": 1}).status_code == 409
    assert client.put(path, json=INPUT | {"revision": 1}).status_code == 409
    assert client.get("/api/projects").json()[0]["name"] == "備品台帳"
    with sessions() as db:
        actions = list(db.scalars(select(Audit.action).where(Audit.resource_id == p["id"])))
        assert actions == ["project.created", "spec.approved", "spec.updated"]


def test_owner_isolation(context):
    client, _ = context
    login(client)
    project = client.post("/api/projects", json=INPUT).json()
    login(client, "bob@example.com")
    assert client.get("/api/projects").json() == []
    assert client.post(f"/api/projects/{project['id']}/approve", json={"revision": 1}).status_code == 404
    assert client.put(f"/api/projects/{project['id']}", json=INPUT | {"revision": 1}).status_code == 404
    assert client.post("/api/users", json={"email": "third@example.com"}).status_code == 403


def test_csrf_and_json_required(context):
    client, _ = context
    login(client)
    assert client.post("/api/projects", json=INPUT, headers={"Origin": "https://evil.test"}).status_code == 403
    assert client.post("/api/projects", data={"name": "test"}).status_code == 415
    client.headers.pop("origin")
    assert client.post("/api/projects", json=INPUT).status_code == 403


def test_disable_revokes_existing_session(context):
    client, sessions = context
    login(client, "bob@example.com")
    assert client.get("/api/me").status_code == 200
    with sessions.begin() as db:
        db.scalar(select(User).where(User.email == "bob@example.com")).enabled = False
    assert client.get("/api/me").status_code == 401


def test_headers_and_unknown_api(context):
    client, _ = context
    result = client.get("/healthz")
    assert result.status_code == 200
    assert result.headers["x-content-type-options"] == "nosniff"
    assert "'unsafe-inline'" not in result.headers["content-security-policy"].split("script-src")[1].split(";")[0]
    assert "img-src 'self' data: blob:" in result.headers["content-security-policy"]
    assert client.get("/api/missing").status_code == 404


def test_invite_and_stop(context):
    client, _ = context
    login(client)
    result = client.post("/api/users", json={"email": "new@example.com", "display_name": "新人",
                                             "role": "member"})
    assert result.status_code == 201
    member = result.json()
    assert member["role"] == "member" and member["display_name"] == "新人"
    # 旧来の全体ロール（developer）はもう受け付けない。テナントごとのロールで決める。
    assert client.post("/api/users", json={"email": "old@example.com", "role": "developer"}).status_code == 422
    stopped = client.patch(f"/api/users/{member['id']}",
                           json={"email": "new@example.com", "display_name": "新人",
                                 "role": "member", "enabled": False})
    assert stopped.json()["enabled"] is False
    assert client.post("/api/users", json={"email": "new@example.com"}).status_code == 409
