import asyncio
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend.config.settings import Settings
from backend.core.db import (AppBuild, AppPublication, Base, Project, PublicationGrant,
    Tenant, TenantMigration, User)
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


@pytest.mark.parametrize('publication_status,build_status,expected', [
    ('running', 'succeeded', 409), ('stopped', 'building', 409),
    ('stopped', 'succeeded', 202)])
def test_stopped_publication_moves_with_build_and_clears_grants(
        migration_app, monkeypatch, publication_status, build_status, expected):
    client, app, (project_id, source_id, target_id) = migration_app
    app.state.settings = app.state.settings.model_copy(update={'publication_enabled': True})
    queued, calls = [], []
    with app.state.sessions.begin() as db:
        admin = db.query(User).filter_by(email='admin@example.com').one()
        build = AppBuild(project_id=project_id, tenant_id=source_id, generation_id=str(uuid4()),
            revision=1, actor_id=admin.id, source_hash='a' * 64, registry_kind='private',
            image='registry.test/app:build', status=build_status)
        db.add(build); db.flush()
        build_id = build.id
        db.add_all([AppPublication(project_id=project_id, build_id=build_id, status=publication_status),
            PublicationGrant(project_id=project_id, kind='department', subject_id=str(uuid4()))])

    async def copy(*args, **kwargs):
        return {'status': 'copied'}

    async def rebind(settings, method, path, payload=None):
        calls.append((method, path, payload))
        return {'status': 'moved'}

    monkeypatch.setattr('backend.api.tenant_migrations.controller_system', copy)
    monkeypatch.setattr('backend.api.tenant_migrations.publication_call', rebind)
    monkeypatch.setattr('backend.api.tenant_migrations.asyncio.create_task',
                        lambda coroutine: queued.append(coroutine))
    response = client.post(f'/api/admin/projects/{project_id}/tenant-migrations', json={
        'target_tenant_id': target_id, 'reason': '公開アプリを移動先テナントへ移します'})
    assert response.status_code == expected, response.text
    if expected == 202:
        asyncio.run(queued.pop())
        with app.state.sessions() as db:
            assert db.get(Project, project_id).tenant_id == target_id
            assert db.get(AppBuild, build_id).tenant_id == target_id
            assert db.query(PublicationGrant).filter_by(project_id=project_id).count() == 0
            assert db.get(AppPublication, project_id).status == 'stopped'
        assert calls == [('POST', f'/projects/{project_id}/tenant-move', {
            'source_tenant_id': source_id, 'target_tenant_id': target_id, 'build_ids': [build_id]})]
    else:
        assert not queued
        assert not calls


def test_failed_publication_rebind_keeps_source_tenant_and_grants(migration_app, monkeypatch):
    client, app, (project_id, source_id, target_id) = migration_app
    app.state.settings = app.state.settings.model_copy(update={'publication_enabled': True})
    queued, calls = [], []
    with app.state.sessions.begin() as db:
        admin = db.query(User).filter_by(email='admin@example.com').one()
        build = AppBuild(project_id=project_id, tenant_id=source_id, generation_id=str(uuid4()),
            revision=1, actor_id=admin.id, source_hash='a' * 64, registry_kind='private',
            image='registry.test/app:build', status='succeeded')
        db.add(build); db.flush()
        build_id = build.id
        db.add_all([AppPublication(project_id=project_id, build_id=build_id, status='stopped'),
            PublicationGrant(project_id=project_id, kind='department', subject_id=str(uuid4()))])

    async def copy(*args, **kwargs):
        return {'status': 'copied'}

    async def rebind(settings, method, path, payload=None):
        calls.append(payload)
        if payload['target_tenant_id'] == target_id:
            raise HTTPException(503, 'controller unavailable')
        return {'status': 'moved'}

    monkeypatch.setattr('backend.api.tenant_migrations.controller_system', copy)
    monkeypatch.setattr('backend.api.tenant_migrations.publication_call', rebind)
    monkeypatch.setattr('backend.api.tenant_migrations.asyncio.create_task',
                        lambda coroutine: queued.append(coroutine))
    response = client.post(f'/api/admin/projects/{project_id}/tenant-migrations', json={
        'target_tenant_id': target_id, 'reason': '公開アプリを移動先テナントへ移します'})
    assert response.status_code == 202
    asyncio.run(queued.pop())
    with app.state.sessions() as db:
        assert db.get(Project, project_id).tenant_id == source_id
        assert db.get(AppBuild, build_id).tenant_id == source_id
        assert db.query(PublicationGrant).filter_by(project_id=project_id).count() == 1
        migration = db.get(TenantMigration, response.json()['id'])
        assert migration.status == 'failed'
        assert '公開基盤' in migration.error
    assert [call['target_tenant_id'] for call in calls] == [target_id, source_id]
