"""生成で選べる Gemini のモデル。管理者が接続先の一覧から選び、選んだものだけが出る。"""
from types import SimpleNamespace
import pytest
from pydantic import SecretStr
from backend.config.settings import Settings
from backend.domain import system_gemini, tenant_llm
from backend.tests.test_api import context  # noqa: F401
from backend.tests.test_generation_api import approved, controller_mock  # noqa: F401
from backend.tests.test_system_gemini import GEMINI, KEY, saved


def test_chosen_models_are_checked_and_deduplicated():
    value = saved({**GEMINI, "generation_models": ["gemini-3.8-flash", "gemini-3.8-flash", "gemini-4.0-pro"]})
    assert value["generation_models"] == ["gemini-3.8-flash", "gemini-4.0-pro"]
    with pytest.raises(ValueError, match="モデル名の形式"):
        system_gemini.SystemGeminiInput(**{**GEMINI, "generation_models": ["Gemini Pro"]})
    with pytest.raises(Exception):
        system_gemini.SystemGeminiInput(**{**GEMINI, "generation_models": [f"gemini-{n}" for n in range(21)]})


def test_chosen_models_replace_the_environment_and_tenants_can_override_them():
    system = saved({**GEMINI, "generation_models": ["gemini-4.0-pro"]})
    assert system_gemini.overrides(system, KEY)["gemini_models"] == "gemini-4.0-pro"
    # 選んでいなければ環境の設定（GEMINI_MODELS）のまま。
    assert "gemini_models" not in system_gemini.overrides(saved(GEMINI), KEY)
    tenant = saved({**GEMINI, "generation_models": ["gemini-3.5-flash"]})
    assert tenant_llm.overrides({"gemini": tenant}, KEY)["gemini_models"] == "gemini-3.5-flash"
    settings = Settings(database_url="postgresql+psycopg://x@localhost/x", app_env="local",
                        app_origin="http://localhost:8080", gemini_models="gemini-3.8-flash,gemini-3.5-flash")
    shown = system_gemini.visible(None, settings, KEY)
    assert shown["generation_models"] == [] and shown["environment"]["generation_models"] == [
        "gemini-3.8-flash", "gemini-3.5-flash"]


def test_only_text_generation_models_become_candidates():
    model = lambda name, actions=("generateContent",), thinking=True, label=None: SimpleNamespace(
        name=name, supported_actions=list(actions) if actions is not None else None, thinking=thinking,
        display_name=label)
    found = system_gemini.generation_candidates([
        model("models/gemini-3.8-flash", label="Gemini 3.8 Flash"),
        model("models/gemini-3.5-flash"),
        model("publishers/google/models/gemini-4.0-pro", actions=None),  # Vertex は対応機能を返さない
        model("models/text-embedding-005", actions=("embedContent",)),
        model("models/gemini-embedding-001"),
        model("models/gemini-3.5-flash-image"),
        model("models/gemini-3.5-flash-tts"),
        model("models/gemini-nano-banana-2.1"),  # 画像生成
        model("models/gemini-3.5-transcribe"),  # 文字起こし
        model("models/gemini-live-3.5-flash"),
        model("models/gemini-3.8-flash", label="duplicate"),
        model("models/gemini-countTokens-only", actions=("countTokens",)),
        model("models/imagen-4.0-generate"),
        model("models/Gemini Bad Name"),
    ])
    assert [item["id"] for item in found] == ["gemini-4.0-pro", "gemini-3.8-flash", "gemini-3.5-flash"]
    assert found[0]["label"] == "gemini-4.0-pro"


def gemini_api_settings(client, models):
    from sqlalchemy import select
    from backend.core import gemini_client
    from backend.core.db import SystemSetting
    from backend.tests.test_api import login
    sessions = client.app.state.sessions

    def loader():
        # アプリが握っている接続ではなく、テストのトランザクションから読む（保存した値が見える）。
        with sessions() as db:
            row = db.scalar(select(SystemSetting).where(SystemSetting.key == system_gemini.KEY))
            return {"gemini": row.value if row else None}
    gemini_client.use_system_settings(loader)
    client.app.state.settings = client.app.state.settings.model_copy(update={
        "tenant_secret_key": SecretStr(KEY), "codex_controller_url": "http://127.0.0.1:8091"})
    login(client)
    response = client.put("/api/system/gemini", json={**GEMINI, "generation_models": models})
    assert response.status_code == 200, response.text
    return response.json()


def test_administrators_list_candidates_with_the_saved_connection(context, monkeypatch):
    client, _ = context

    async def controller_system(*args, **kwargs):
        return {}
    monkeypatch.setattr("backend.api.system_settings.controller_system", controller_system)
    gemini_api_settings(client, [])
    used = []

    def candidates(settings=None, *, api_key=None):
        used.append(api_key)
        return [{"id": "gemini-3.5-flash", "label": "Gemini 3.5 Flash", "thinking": True},
                {"id": "gemini-4.0-pro", "label": "Gemini 4.0 Pro", "thinking": True}]
    monkeypatch.setattr("backend.core.gemini_client.generation_candidates", candidates)
    body = client.post("/api/system/gemini/models", json={}).json()
    # 思考レベルを選べるのは対応表にあるモデルだけ。
    assert body["models"][0]["thinking_levels"] == ["minimal", "low", "medium", "high"]
    assert body["models"][1]["thinking_levels"] == []
    assert used == [None]

    def broken(settings=None, *, api_key=None):
        raise RuntimeError("upstream text must not leak AIza-secret")
    monkeypatch.setattr("backend.core.gemini_client.generation_candidates", broken)
    failed = client.post("/api/system/gemini/models", json={})
    assert failed.status_code == 503 and "AIza" not in failed.text

    from backend.tests.test_api import login
    login(client, "bob@example.com")
    assert client.post("/api/system/gemini/models", json={}).status_code == 403


def test_tenant_candidates_use_the_tenant_key_and_explain_vertex(context, monkeypatch):
    from sqlalchemy import select
    from backend.core.db import Tenant, TenantLlmSetting
    client, sessions = context
    gemini_api_settings(client, [])
    with sessions() as db:
        tenant_id = db.scalar(select(Tenant.id))
    used = []
    monkeypatch.setattr("backend.core.gemini_client.generation_candidates",
                        lambda settings=None, *, api_key=None: used.append(api_key) or [])
    tenant_key = saved({**GEMINI, "api_key": "AIza-tenant-key"})
    with sessions.begin() as db:
        db.add(TenantLlmSetting(tenant_id=tenant_id, kind="gemini", value=tenant_key))
    assert client.post(f"/api/tenants/{tenant_id}/llm/gemini/models", json={}).status_code == 200
    assert used == ["AIza-tenant-key"]
    with sessions.begin() as db:
        row = db.scalar(select(TenantLlmSetting).where(TenantLlmSetting.tenant_id == tenant_id))
        row.value = {**tenant_key, "backend": "vertex"}
    refused = client.post(f"/api/tenants/{tenant_id}/llm/gemini/models", json={})
    assert refused.status_code == 409 and "モデルのID" in refused.json()["error"]


def test_generation_accepts_only_the_models_an_administrator_offered(context, controller_mock, monkeypatch):
    from backend.core import gemini_client
    client, _ = context

    async def controller_system(*args, **kwargs):
        return {}
    monkeypatch.setattr("backend.api.system_settings.controller_system", controller_system)
    gemini_api_settings(client, ["gemini-3.5-flash"])
    gemini_client.refresh_system_settings()
    path = approved(client)
    models = {m["id"] for m in client.get("/api/codex/models").json()["models"] if m["provider"] == "gemini"}
    assert models == {"gemini-3.5-flash"}
    refused = client.post(path + "/generate", json={"provider": "gemini", "model": "gemini-3.8-flash"})
    assert refused.status_code == 422 and "選択肢に無い" in refused.json()["error"]
    accepted = client.post(path + "/generate", json={"provider": "gemini", "model": "gemini-3.5-flash"})
    assert accepted.status_code == 202, accepted.text
