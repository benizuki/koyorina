"""アプリの削除。DB・実行環境・Codexの作業場所をまとめて片付ける。"""
from backend.core.db import AppBuild, AppPublication, Audit, GenerationJob, Project, PublicationEvent
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


def built(sessions, project_id, status="succeeded"):
    """一度ビルドし、公開・停止までした記録を作る。"""
    with sessions.begin() as db:
        record = db.get(Project, project_id)
        build = AppBuild(project_id=record.id, tenant_id=record.tenant_id, generation_id=str(record.id),
                         revision=1, actor_id=record.owner_id, source_hash="0" * 64,
                         registry_kind="private", image="registry.test/app:1", status=status)
        db.add(build)
        db.flush()
        db.add(PublicationEvent(project_id=record.id, build_id=build.id, actor_id=record.owner_id,
                                action="release"))
        return build.id


def test_a_project_with_build_history_can_be_deleted_once_its_publication_is_gone(context):
    """公開アプリを削除した後は、ビルド履歴が残っていてもプロジェクトを消せる。"""
    client, sessions = context
    login(client)
    project = client.post("/api/projects", json=INPUT).json()
    build_id = built(sessions, project["id"])
    with sessions.begin() as db:
        db.add(AppPublication(project_id=project["id"], build_id=build_id, status="stopped"))
    refused = client.request("DELETE", f"/api/projects/{project['id']}", json={})
    # 公開アプリが残っている間は、プロジェクトの削除で巻き込まない。
    assert refused.status_code == 409 and "公開アプリ" in refused.json()["error"]
    with sessions.begin() as db:
        db.delete(db.get(AppPublication, project["id"]))
    result = client.request("DELETE", f"/api/projects/{project['id']}", json={})
    assert result.status_code == 200, result.text
    with sessions() as db:
        assert db.get(Project, project["id"]) is None
        assert db.get(AppBuild, build_id) is None
        assert db.query(PublicationEvent).filter(PublicationEvent.project_id == project["id"]).count() == 0
        assert db.query(Audit).filter(Audit.action == "project.deleted",
                                      Audit.resource_id == project["id"]).count() == 1


def test_delete_waits_for_a_running_build(context):
    client, sessions = context
    login(client)
    project = client.post("/api/projects", json=INPUT).json()
    built(sessions, project["id"], status="building")
    refused = client.request("DELETE", f"/api/projects/{project['id']}", json={})
    assert refused.status_code == 409 and "ビルド中" in refused.json()["error"]
