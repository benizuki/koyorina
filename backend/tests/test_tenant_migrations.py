import asyncio

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend.config.settings import Settings
from backend.core.db import Base, Project, Tenant, TenantMigration, User
from backend.main import create_app


@pytest.fixture
def migration_app(tmp_path, monkeypatch):
    settings = Settings(_env_file=None, app_env="production",
        database_url="postgresql+psycopg://test:test@localhost/test",
        app_origin="https://forge.test",
        app_session_secret="test-only-session-secret-at-least-32-characters",
        google_oauth_client_id="test-client")
    settings = settings.model_copy(update={"database_url": f"sqlite:///{tmp_path / 'migration.db'}"})
    app = create_app(settings)
    Base.metadata.create_all(app.state.sessions.kw["bind"])
    with app.state.sessions.begin() as db:
        admin = User(email="admin@example.com", role="admin")
        source, target = Tenant(name="Source"), Tenant(name="Target")
        db.add_all([admin, source, target])
        db.flush()
        project = Project(owner_id=admin.id, tenant_id=source.id, name="App",
                          purpose="Move data", audience="self", fields=[], tables=[], requirements=[])
        db.add(project)
        db.flush()
        ids = project.id, source.id, target.id
    monkeypatch.setattr("backend.main.id_token.verify_oauth2_token", lambda *args, **kwargs:
        {"email": "admin@example.com", "email_verified": True,
         "iss": "https://accounts.google.com"})
    with TestClient(app, base_url="https://forge.test",
                    headers={"Origin": "https://forge.test"}) as client:
        assert client.post("/auth/google", json={"credential": "admin@example.com"}).status_code == 200
        yield client, app, ids


@pytest.mark.parametrize("copy_succeeds", [True, False])
def test_tenant_move_switches_db_only_after_verified_copy(migration_app, monkeypatch, copy_succeeds):
    client, app, (project_id, source_id, target_id) = migration_app
    queued = []

    async def copy(*args, **kwargs):
        if not copy_succeeds:
            raise HTTPException(503, "copy failed")
        return {"status": "copied"}

    monkeypatch.setattr("backend.api.tenant_migrations.controller_system", copy)
    monkeypatch.setattr("backend.api.tenant_migrations.asyncio.create_task",
                        lambda coroutine: queued.append(coroutine))
    response = client.post(f"/api/admin/projects/{project_id}/tenant-migrations", json={
        "target_tenant_id": target_id, "reason": "組織再編に伴う保存領域の移行"})
    assert response.status_code == 202
    assert len(queued) == 1
    asyncio.run(queued.pop())

    with app.state.sessions() as db:
        project = db.get(Project, project_id)
        migration = db.get(TenantMigration, response.json()["id"])
        if copy_succeeds:
            assert project.tenant_id == target_id
            assert migration.status == "completed"
            assert migration.source_retained_until is not None
        else:
            assert project.tenant_id == source_id
            assert migration.status == "failed"
            assert migration.source_retained_until is None
