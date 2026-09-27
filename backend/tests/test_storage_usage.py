from fastapi.testclient import TestClient

from backend.config.settings import Settings
from backend.core.db import Base, StorageUsageSnapshot, Tenant, User
from backend.main import create_app


def test_platform_admin_refreshes_and_reads_tenant_storage(tmp_path, monkeypatch):
    settings = Settings(_env_file=None, app_env="production",
        database_url="postgresql+psycopg://test:test@localhost/test", app_origin="https://forge.test",
        app_session_secret="test-only-session-secret-at-least-32-characters",
        google_oauth_client_id="test-client")
    settings = settings.model_copy(update={"database_url": f"sqlite:///{tmp_path / 'storage.db'}"})
    app = create_app(settings)
    Base.metadata.create_all(app.state.sessions.kw["bind"])
    with app.state.sessions.begin() as db:
        admin, tenant = User(email="admin@example.com", role="admin"), Tenant(name="営業")
        db.add_all([admin, tenant]); db.flush(); tenant_id = tenant.id

    monkeypatch.setattr("backend.main.id_token.verify_oauth2_token", lambda *args, **kwargs:
        {"email": "admin@example.com", "email_verified": True, "iss": "https://accounts.google.com"})

    async def measured(_settings, _tenant):
        return [
            {"kind": "generation", "claim_name": "generation", "status": "ok",
             "requested_bytes": 1000, "used_bytes": 850, "capacity_bytes": 10000,
             "available_bytes": 1500, "measured_at": "2026-09-20T00:00:00+00:00"},
            {"kind": "preview", "claim_name": "preview", "status": "missing",
             "requested_bytes": 1000, "used_bytes": 0, "capacity_bytes": 0,
             "available_bytes": 0, "measured_at": "2026-09-20T00:00:00+00:00"}]
    monkeypatch.setattr("backend.api.storage_usage.measure_pair", measured)

    with TestClient(app, base_url="https://forge.test", headers={"Origin": "https://forge.test"}) as client:
        assert client.post("/auth/google", json={"credential": "admin@example.com"}).status_code == 200
        refreshed = client.post("/api/admin/storage-usage", json={})
        assert refreshed.status_code == 200
        row = refreshed.json()["tenants"][0]
        assert row["tenant_id"] == tenant_id and row["generation"]["level"] == "warning"
        assert refreshed.json()["node"]["level"] == "warning"
        cached = client.get("/api/admin/storage-usage").json()
        assert cached["totals"]["used_bytes"] == 850

    with app.state.sessions() as db:
        assert db.get(StorageUsageSnapshot, (tenant_id, "generation")).used_bytes == 850
