import json

import pytest
from sqlalchemy import select

from backend.core.db import Audit, Project, TenantAiSettings
from backend.tests.test_api import context, login, INPUT  # noqa: F401
from backend.tests.test_preview import generated_job, preview  # noqa: F401
from backend.tests.test_tenant_ai import KEY, VERTEX

GEMINI = {"backend": "gemini_api", "model": "gemini-3.5-flash", "thinking_level": "LOW",
          "api_key": "AIza-test-only-value"}


def allow_secrets(client):
    client.app.state.settings = client.app.state.settings.model_copy(
        update={"tenant_secret_key": __import__("pydantic").SecretStr(KEY)})


def tenant_of(client, sessions):
    login(client)
    project = client.post("/api/projects", json=INPUT).json()
    with sessions() as db:
        return db.get(Project, project["id"]).tenant_id


def test_only_administrators_manage_tenant_gemini(context):
    client, sessions = context
    tenant_id = tenant_of(client, sessions)
    login(client, "bob@example.com")
    assert client.get(f"/api/tenants/{tenant_id}/ai-settings").status_code == 403
    assert client.put(f"/api/tenants/{tenant_id}/ai-settings", json=VERTEX).status_code == 403


def test_the_api_key_is_stored_sealed_and_never_returned(context):
    client, sessions = context
    tenant_id = tenant_of(client, sessions)
    allow_secrets(client)
    response = client.put(f"/api/tenants/{tenant_id}/ai-settings", json=GEMINI)
    assert response.status_code == 200, response.text
    assert response.json()["api_key_configured"] is True
    assert GEMINI["api_key"] not in response.text
    assert GEMINI["api_key"] not in client.get(f"/api/tenants/{tenant_id}/ai-settings").text
    with sessions() as db:
        saved = db.get(TenantAiSettings, tenant_id)
        assert saved.api_key_encrypted and GEMINI["api_key"] not in saved.api_key_encrypted
        details = [audit.detail for audit in db.scalars(select(Audit).where(
            Audit.action == "tenant.ai_settings_updated", Audit.resource_id == tenant_id))]
        assert "backend=gemini_api" in details
        assert all(GEMINI["api_key"] not in (detail or "") for detail in details)
    # キーを送らずに他を直しても、キーは残る。
    update = {k: v for k, v in GEMINI.items() if k != "api_key"} | {"model": "gemini-3.8-flash"}
    assert client.put(f"/api/tenants/{tenant_id}/ai-settings", json=update).json()["api_key_configured"]


def test_without_a_sealing_key_api_keys_are_refused_but_vertex_works(context):
    client, sessions = context
    tenant_id = tenant_of(client, sessions)
    refused = client.put(f"/api/tenants/{tenant_id}/ai-settings", json=GEMINI)
    assert refused.status_code == 422 and "TENANT_SECRET_KEY" in refused.json()["error"]
    accepted = client.put(f"/api/tenants/{tenant_id}/ai-settings", json=VERTEX)
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["secrets_available"] is False


def test_generated_apps_receive_the_tenant_gemini_and_can_override_it(preview):
    client, sessions, calls, state = preview
    project_id, job_id = generated_job(sessions, client)
    allow_secrets(client)
    with sessions() as db:
        tenant_id = db.get(Project, project_id).tenant_id
    assert client.put(f"/api/tenants/{tenant_id}/ai-settings", json=GEMINI).status_code == 200
    # アプリ側でモデルだけ上書きする。
    client.put(f"/api/projects/{project_id}/preview/env",
               json={"entries": [{"name": "GEMINI_MODEL", "value": "gemini-3.8-flash", "secret": False}]})
    listed = client.get(f"/api/projects/{project_id}/preview/env").json()["entries"]
    inherited = {item["name"]: item for item in listed if item.get("inherited")}
    assert inherited["GEMINI_MODEL"]["overridden"] is True
    assert inherited["GEMINI_API_KEY"]["value"] is None and GEMINI["api_key"] not in json.dumps(listed)
    # 受け継いだ項目を含めて送り返しても、プロジェクトの指定としては保存しない。
    saved = client.put(f"/api/projects/{project_id}/preview/env", json={"entries": listed})
    assert saved.status_code == 200, saved.text
    with sessions() as db:
        assert [item["name"] for item in db.get(Project, project_id).preview_env] == ["GEMINI_MODEL"]

    assert client.post(f"/api/projects/{project_id}/jobs/{job_id}/preview", json={}).status_code == 202
    environment = [call for call in calls if call[0] == "run"][-1][3]
    assert environment["GEMINI_MODEL"] == "gemini-3.8-flash"         # アプリの上書きが勝つ
    assert environment["GEMINI_THINKING_LEVEL"] == "LOW"             # テナントの既定値
    assert environment["GOOGLE_GENAI_USE_VERTEXAI"] == "false"
    assert environment["GEMINI_API_KEY"] == GEMINI["api_key"]        # ローカルのdockerでは環境変数で渡す
    assert environment["APP_BASE_PATH"] == f"/apps/{project_id}/"    # Koyorinaの予約値は変わらない


def test_generation_is_told_that_gemini_is_available(context, monkeypatch):
    from backend.tests.test_generation_api import approved
    client, sessions = context
    sent = []

    async def controller(settings, user_id, method, path, body=None, **kwargs):
        sent.append((path, body))
        if path == "/account":
            return {"status": "connected", "generator": "gemini"}
        if "/attachments" in path:
            return {"attachments": []}
        return {"status": "generating"}

    monkeypatch.setattr("backend.api.generation.controller", controller)
    path = approved(client)
    with sessions() as db:
        tenant_id = db.get(Project, path.rsplit("/", 1)[1]).tenant_id
    client.put(f"/api/tenants/{tenant_id}/ai-settings", json=VERTEX)
    assert client.post(path + "/generate", json={"provider": "gemini"}).status_code == 202
    body = next(body for sent_path, body in sent if sent_path == "/jobs/start")
    assert body["specification"]["llm"] == {"available": True, "backend": "vertex"}
    # 接続先の中身は生成AIへ渡さない。
    assert "customer-ai-123" not in json.dumps(body) and "koyorina-pool" not in json.dumps(body)
    # 仕様としては保存しない。
    with sessions() as db:
        assert "llm" not in json.dumps(db.get(Project, path.rsplit("/", 1)[1]).tables or [])
