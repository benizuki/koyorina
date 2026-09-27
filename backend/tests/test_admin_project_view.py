"""Admin read access must not grant owner mutations or bypass normal isolation."""
import pytest
from fastapi.testclient import TestClient
from backend.config.settings import Settings
from backend.core.db import Base, GenerationJob, Project, Tenant, User, UserTenant
from backend.main import create_app


@pytest.fixture
def context(tmp_path, monkeypatch):
    settings = Settings(_env_file=None, app_env="production",
        database_url="postgresql+psycopg://test:test@localhost/test", app_origin="https://forge.test",
        app_session_secret="test-only-session-secret-at-least-32-characters",
        google_oauth_client_id="test-client")
    # Isolate route/authorization tests without changing production PostgreSQL validation.
    settings = settings.model_copy(update={"database_url": f"sqlite:///{tmp_path / 'test.db'}"})
    app = create_app(settings)
    Base.metadata.create_all(app.state.sessions.kw["bind"])
    with app.state.sessions.begin() as db:
        owner = User(email="owner@example.com", role="developer", display_name="Owner")
        db.add_all([owner, User(email="admin@example.com", role="admin"),
                    User(email="other@example.com", role="developer"),
                    User(email="reader@example.com", role="user")]); db.flush()
        tenant = Tenant(name="Tenant A"); db.add(tenant); db.flush()
        db.add(UserTenant(user_id=owner.id, tenant_id=tenant.id))
        project = Project(owner_id=owner.id, tenant_id=tenant.id, name="Draft", purpose="Review a draft",
                          audience="self", fields=[], tables=[], requirements=[])
        db.add(project); db.flush()
        job = GenerationJob(project_id=project.id, owner_id=owner.id, revision=1,
                            specification={}, instruction="Create draft", status="generated")
        db.add(job); db.flush()
        admin = db.query(User).filter_by(email="admin@example.com").one()
        ids = project.id, job.id, owner.id, admin.id
    monkeypatch.setattr("backend.main.id_token.verify_oauth2_token", lambda token, *args:
        {"email": token, "email_verified": True, "iss": "https://accounts.google.com"})
    with TestClient(app, base_url="https://forge.test", headers={"Origin": "https://forge.test"}) as client:
        yield client, ids


def login(client, email):
    assert client.post("/auth/google", json={"credential": email}).status_code == 200


def test_admin_reads_list_history_and_owner_workspace(context, monkeypatch):
    client, (pid, jid, owner_id, admin_id) = context
    calls = []
    async def controller(settings, user_id, method, path, *args, **kwargs):
        calls.append((user_id, method, path))
        return {"status": "listed", "files": [], "truncated": False}
    monkeypatch.setattr("backend.api.generation.controller", controller)
    login(client, "admin@example.com")
    projects = client.get("/api/projects").json()
    assert len(projects) == 1 and projects[0]["id"] == pid
    assert projects[0]["owner_id"] == owner_id and projects[0]["owner_name"] == "Owner"
    assert client.get(f"/api/projects/{pid}/jobs").json()[0]["id"] == jid
    assert client.get(f"/api/projects/{pid}/files").status_code == 403
    tenant_id = projects[0]["tenant_id"]
    support = client.post("/api/admin/support-sessions", json={"tenant_id": tenant_id,
        "permission": "inspect", "reason": "利用者から依頼された生成結果の調査"})
    assert support.status_code == 201
    assert client.get(f"/api/projects/{pid}/files").status_code == 200
    # 作業場所はアプリ単位の共有領域にある。自分のPodへ聞いても同じものが読める。
    assert calls == [(admin_id, "GET", f"/projects/{pid}/files")]
    # 管理者は見るだけ。全員のアプリを書き換えられると、誰が変えたのか辿れなくなる。
    assert client.post(f"/api/projects/{pid}/approve", json={"revision": 1}).status_code == 404
    assert client.post(f"/api/projects/{pid}/generate", json={}).status_code == 404
    # inspectではソースを削除できない。repairを理由付きで開始してから片付ける。
    assert client.request("DELETE", f"/api/projects/{pid}", json={}).status_code == 403
    assert client.post("/api/admin/support-sessions", json={"tenant_id": tenant_id,
        "permission": "repair", "reason": "利用者から依頼されたプロジェクト削除対応"}).status_code == 201
    assert client.request("DELETE", f"/api/projects/{pid}", json={}).status_code == 200
    assert client.get("/api/projects").json() == []


@pytest.mark.parametrize("email", ["other@example.com", "reader@example.com"])
def test_non_admin_cannot_read_other_users_projects(context, email):
    client, (pid, _, _, _) = context
    login(client, email)
    assert client.get("/api/projects").json() == []
    assert client.get(f"/api/projects/{pid}/jobs").status_code == 404
    assert client.get(f"/api/projects/{pid}/files").status_code == 404


def test_owner_and_unauthenticated_access(context):
    client, (pid, jid, _, _) = context
    assert client.get("/api/projects").status_code == 401
    login(client, "owner@example.com")
    assert client.get("/api/projects").json()[0]["id"] == pid
    assert client.get(f"/api/projects/{pid}/jobs").json()[0]["id"] == jid
    assert client.post(f"/api/projects/{pid}/approve", json={"revision": 1}).status_code == 200


def test_support_permission_is_scoped_and_revocable(context, monkeypatch):
    client, (pid, _, _, _) = context
    calls = []

    async def controller(settings, user_id, method, path, body=None, **kwargs):
        calls.append((method, path))
        return {"attachments": []} if method == "GET" else {"status": "added"}

    monkeypatch.setattr("backend.api.generation.controller", controller)
    login(client, "admin@example.com")
    tenant_id = client.get("/api/projects").json()[0]["tenant_id"]
    inspect = client.post("/api/admin/support-sessions", json={"tenant_id": tenant_id,
        "permission": "inspect", "reason": "利用者から依頼された添付資料の確認"}).json()
    assert client.get(f"/api/projects/{pid}/attachments").status_code == 200
    refused = client.post(f"/api/projects/{pid}/attachments", content=b"hello",
                          headers={"Content-Type": "application/octet-stream",
                                   "X-File-Name": "note.txt"})
    assert refused.status_code == 403
    assert client.request("DELETE", f"/api/admin/support-sessions/{inspect['id']}", json={}).status_code == 200
    assert client.get(f"/api/projects/{pid}/attachments").status_code == 403

    repair = client.post("/api/admin/support-sessions", json={"tenant_id": tenant_id,
        "permission": "repair", "reason": "利用者から依頼された添付資料の修復"}).json()
    added = client.post(f"/api/projects/{pid}/attachments", content=b"hello",
                        headers={"Content-Type": "application/octet-stream",
                                 "X-File-Name": "note.txt"})
    assert added.status_code == 200
    assert client.request("DELETE", f"/api/admin/support-sessions/{repair['id']}", json={}).status_code == 200
    assert calls == [("GET", f"/projects/{pid}/attachments"),
                     ("POST", f"/projects/{pid}/attachments")]


def test_the_file_tab_downloads_as_one_zip(context, monkeypatch):
    import io
    import zipfile
    client, (pid, _, _, _) = context
    calls = []

    async def controller(settings, user_id, method, path, *args, **kwargs):
        calls.append(path)
        return {"status": "collected", "truncated": False, "files": [
            {"path": "backend/main.py", "content": "x = 1\n"},
            {"path": "frontend/src/app.vue", "content": "<template/>"},
            {"path": "../outside.py", "content": "no"}]}
    monkeypatch.setattr("backend.api.generation.controller", controller)
    login(client, "owner@example.com")
    response = client.get(f"/api/projects/{pid}/files/archive")
    assert response.status_code == 200 and response.headers["content-type"] == "application/zip"
    assert calls == [f"/projects/{pid}/archive"]
    names = zipfile.ZipFile(io.BytesIO(response.content)).namelist()
    # 作業場所から来たパスも確かめ直す。外を指す名前はZIPに入れない。
    assert names == ["backend/main.py", "frontend/src/app.vue"]


def test_others_cannot_download_the_files(context):
    client, (pid, _, _, _) = context
    login(client, "other@example.com")
    assert client.get(f"/api/projects/{pid}/files/archive").status_code == 404
