"""テナントの生成AI。テナントの値がシステムの既定より優先し、無ければ既定、使わせない指定もできる。"""
import asyncio
import json
from uuid import UUID

import pytest
from pydantic import SecretStr
from sqlalchemy import select

from backend.core.db import Audit, TenantLlmSetting
from backend.domain import system_gemini, system_llm, tenant_llm
from backend.tests.test_api import context, login  # noqa: F401
from backend.tests.test_system_gemini import VERTEX, controller_settings
from backend.tests.test_system_llm import COMPATIBLE
from backend.tests.test_tenant_admins import two_tenants
from backend.tests.test_tenant_ai import KEY

TENANT = str(UUID(int=7))
TENANT_VERTEX = {**VERTEX, "gcp_project": "tenant-project-01", "wif_project_number": "123456789012",
                 "wif_pool_id": "tenant-pool", "wif_provider_id": "tenant-provider"}


def with_controller(client, monkeypatch):
    client.app.state.settings = client.app.state.settings.model_copy(
        update={"codex_controller_url": "http://127.0.0.1:8091", "tenant_secret_key": SecretStr(KEY)})
    sent = []

    async def controller_system(settings, method, path, body=None, timeout=420):
        sent.append((path, body))
        return {}
    monkeypatch.setattr("backend.api.tenant_llm.controller_system", controller_system)
    return sent


def test_tenant_values_are_saved_sent_and_never_returned(context, monkeypatch):
    client, sessions = context
    first, _, _ = two_tenants(sessions)
    sent = with_controller(client, monkeypatch)
    login(client)
    response = client.put(f"/api/tenants/{first}/llm/openai_compatible",
                          json={"mode": "tenant", "settings": COMPATIBLE})
    assert response.status_code == 200, response.text
    assert response.json()["mode"] == "tenant" and "sk-compatible" not in response.text
    path, body = sent[-1]
    assert path == f"/settings/tenants/{first}/llm"
    assert body["openai_compatible"]["api_key"] == "sk-compatible-only"
    # 使わせない指定と、既定に戻す指定。
    client.put(f"/api/tenants/{first}/llm/gemini", json={"mode": "disabled"})
    assert sent[-1][1]["disabled"] == ["gemini"]
    listed = client.get(f"/api/tenants/{first}/llm").json()
    assert listed["gemini"]["mode"] == "disabled" and listed["antigravity"]["mode"] == "system"
    client.put(f"/api/tenants/{first}/llm/gemini", json={"mode": "system"})
    assert client.get(f"/api/tenants/{first}/llm").json()["gemini"]["mode"] == "system"
    with sessions() as db:
        value = db.get(TenantLlmSetting, (first, "openai_compatible")).value
        assert "sk-compatible" not in json.dumps(value)
        details = [a.detail for a in db.scalars(select(Audit).where(Audit.action == "tenant.llm_updated"))]
        assert all("sk-" not in (d or "") for d in details)


def test_only_that_tenants_admins_change_it(context, monkeypatch):
    client, sessions = context
    first, second, bob = two_tenants(sessions)
    with_controller(client, monkeypatch)
    login(client)
    client.patch(f"/api/users/{bob}", json={"email": "bob@example.com", "role": "member",
                                            "tenants": [{"tenant_id": first, "role": "admin"},
                                                        {"tenant_id": second, "role": "developer"}]})
    login(client, "bob@example.com")
    assert client.put(f"/api/tenants/{first}/llm/gemini", json={"mode": "disabled"}).status_code == 200
    assert client.put(f"/api/tenants/{second}/llm/gemini", json={"mode": "disabled"}).status_code == 403
    assert client.get(f"/api/tenants/{second}/llm").status_code == 403


def test_model_choices_follow_the_tenant(context, monkeypatch):
    client, sessions = context
    first, second, _ = two_tenants(sessions)
    with_controller(client, monkeypatch)
    login(client)
    client.put(f"/api/tenants/{first}/llm/openai_compatible", json={"mode": "tenant", "settings": COMPATIBLE})
    providers = {o["provider"] for o in client.get(f"/api/codex/models?tenant_id={first}").json()["models"]}
    assert "openai_compatible" in providers
    others = {o["provider"] for o in client.get(f"/api/codex/models?tenant_id={second}").json()["models"]}
    assert "openai_compatible" not in others


def run_effective(system=None, tenant=None):
    from backend.worker.controller import Provisioner
    provisioner = Provisioner(controller_settings())

    async def kube(method, resource, name="", body=None, query="", text=False):
        if resource != "configmaps":
            return None
        if "-tenant-llm-" in name:
            return {"data": tenant} if tenant else None
        return {"data": system} if system and name.endswith("system-gemini") else None
    provisioner.kube = kube
    return asyncio.run(provisioner.effective(TENANT))


def controller_data(payload_rows):
    """main app の送る形を、controller が ConfigMap に書く形にする（set_tenant_llm と同じ規則）。"""
    from backend.worker.controller import Provisioner, TenantLlmInput
    written = {}
    provisioner = Provisioner(controller_settings())

    async def kube(method, resource, name="", body=None, query="", text=False):
        if method == "POST":
            written[(resource, body["metadata"]["name"])] = body
    provisioner.kube = kube
    provisioner.retire_stale_soon = lambda: None
    asyncio.run(provisioner.set_tenant_llm(TENANT, TenantLlmInput(**tenant_llm.controller_payload(payload_rows, KEY))))
    return written


def test_tenant_vertex_uses_its_own_identity_and_project():
    from backend.worker.controller import agent_service_account, resources
    rows = {"gemini": system_gemini.apply(None, system_gemini.SystemGeminiInput(**TENANT_VERTEX), KEY)}
    written = controller_data(rows)
    config = next(body for (kind, _), body in written.items() if kind == "configmaps")
    settings, wif = run_effective(tenant=config["data"])
    assert settings.vertex_project == "tenant-project-01"
    assert "tenant-pool" in wif["audience"]
    pod = resources(UUID(int=1), settings, "shared", tenant=TENANT, wif=wif)["pods"]["spec"]
    assert pod["serviceAccountName"] == agent_service_account(settings)
    assert "-agent-t-" in pod["serviceAccountName"]
    vertex = next(v for v in pod["volumes"] if v["name"] == "vertex")
    assert vertex["projected"]["sources"][1]["configMap"]["name"] == config["metadata"]["name"]


def test_disabling_gemini_for_a_tenant_hides_the_system_default():
    from backend.worker.controller import worker_route
    from fastapi import HTTPException
    settings, wif = run_effective(system={"backend": "developer", "model": "m"}, tenant={"gemini.disabled": "true"})
    assert wif is None
    with pytest.raises(HTTPException):
        worker_route(settings, "gemini")


def test_without_tenant_values_the_system_default_is_used():
    settings, _ = run_effective(system={"backend": "developer", "model": "system-model"})
    assert settings.gemini_api_backend == "developer" and settings.gemini_model == "system-model"


def test_tenant_keys_go_to_their_own_secrets():
    rows = {"openai_compatible": system_llm.apply(None, system_llm.OpenAICompatibleInput(**COMPATIBLE), KEY,
                                                  key_required=True)}
    written = controller_data(rows)
    secrets = {name: body for (kind, name), body in written.items() if kind == "secrets"}
    assert len(secrets) == 1 and next(iter(secrets)).endswith("-openai-compatible")
    config = next(body for (kind, _), body in written.items() if kind == "configmaps")
    assert "sk-compatible" not in json.dumps(config)
