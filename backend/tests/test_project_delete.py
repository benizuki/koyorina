"""アプリの削除。DB・実行環境・Codexの作業場所をまとめて片付ける。"""
from backend.core.db import Audit, GenerationJob, Project
from backend.tests.test_api import context, login
from backend.tests.test_projects import INPUT

def test_delete_removes_the_app_and_its_history(context, monkeypatch):
    """削除はDBだけでなく、実行環境とCodexの作業場所も片付ける。"""
    client, sessions = context
    login(client)
    project = client.post("/api/projects", json=INPUT).json()
    with sessions.begin() as db:
        record = db.get(Project, project["id"])
        db.add(GenerationJob(project_id=record.id, owner_id=record.owner_id, revision=1,
                             specification=INPUT, status="generated"))
    cleaned = []

    async def codex(settings, user_id, method, path, body=None, **kwargs):
        cleaned.append((path, body))
        return {"removed": 2}

    async def discard(self, identifier):
        cleaned.append(("preview", identifier))
        return {"state": "stopped"}

    monkeypatch.setattr("backend.api.projects.controller", codex)
    monkeypatch.setattr("backend.core.preview_backend.DockerBackend.discard", discard)
    client.app.state.settings = client.app.state.settings.model_copy(
        update={"codex_controller_url": "http://127.0.0.1:8091"})
    result = client.request("DELETE", f"/api/projects/{project['id']}", json={})
    assert result.status_code == 200 and result.json() == {"deleted": True, "remaining": []}
    assert cleaned[0][0] == f"/projects/{project['id']}/remove"
    assert len(cleaned[0][1]["job_ids"]) == 1
    with sessions() as db:
        assert db.get(Project, project["id"]) is None
        assert db.query(GenerationJob).filter(GenerationJob.project_id == project["id"]).count() == 0
        # 監査は残す。何が消えたかを後から追えるようにする。
        assert db.query(Audit).filter(Audit.action == "project.deleted",
                                      Audit.resource_id == project["id"]).count() == 1
    assert client.get(f"/api/projects/{project['id']}").status_code == 404


def test_delete_is_refused_while_generating_and_for_other_owners(context):
    client, sessions = context
    login(client)
    project = client.post("/api/projects", json=INPUT).json()
    with sessions.begin() as db:
        record = db.get(Project, project["id"])
        db.add(GenerationJob(project_id=record.id, owner_id=record.owner_id, revision=1,
                             specification=INPUT, status="generating"))
    assert client.request("DELETE", f"/api/projects/{project['id']}", json={}).status_code == 409
    login(client, "bob@example.com")
    assert client.request("DELETE", f"/api/projects/{project['id']}", json={}).status_code == 404
