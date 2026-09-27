"""システム設定の Antigravity と OpenAI 互換 API。環境の設定より優先し、キーは暗号化して持つ。"""
import asyncio
import json
from uuid import UUID

import pytest
from pydantic import SecretStr, ValidationError

from backend.core import gemini_client, secret_box
from backend.domain import system_llm
from backend.tests.test_system_gemini import Env, controller_settings
from backend.tests.test_tenant_ai import KEY

ANTIGRAVITY = {"enabled": True, "model": "gemini-3.8-flash", "agent": "antigravity-preview-09-2026",
               "max_total_tokens": 60000, "api_key": "AIza-antigravity-only"}
COMPATIBLE = {"enabled": True, "label": "社内LLM", "base_url": "https://llm.example.com/v1",
              "model": "qwen-coder", "api_key": "sk-compatible-only"}


def saved(kind, values, stored=None):
    parsed = system_llm.INPUTS[kind](**values)
    return system_llm.apply(stored, parsed, KEY, key_required=kind == system_llm.OPENAI_COMPATIBLE)


@pytest.mark.parametrize("change", [
    {"base_url": "ftp://llm.example.com"}, {"base_url": "https://user:pass@llm.example.com"},
    {"base_url": "https://llm.example.com/v1?key=x"}, {"model": ""},
])
def test_the_openai_compatible_endpoint_is_checked(change):
    with pytest.raises(ValidationError):
        system_llm.OpenAICompatibleInput(**{**COMPATIBLE, **change})


def test_disabling_needs_nothing_else():
    assert system_llm.OpenAICompatibleInput(enabled=False).enabled is False
    assert system_llm.AntigravityInput(enabled=False).enabled is False


def test_keys_are_sealed_kept_and_required_where_needed():
    value = saved(system_llm.OPENAI_COMPATIBLE, COMPATIBLE)
    assert "sk-compatible" not in json.dumps(value)
    assert secret_box.open_(KEY, value["api_key_encrypted"]) == "sk-compatible-only"
    kept = saved(system_llm.OPENAI_COMPATIBLE, {**COMPATIBLE, "api_key": None}, value)
    assert kept["api_key_encrypted"] == value["api_key_encrypted"]
    with pytest.raises(Exception, match="APIキー"):
        saved(system_llm.OPENAI_COMPATIBLE, {**COMPATIBLE, "api_key": None})
    # Antigravity はキーを入れなくてもよい（環境のキーを使う）。
    assert saved(system_llm.ANTIGRAVITY, {**ANTIGRAVITY, "api_key": None})["api_key_encrypted"] is None


def test_the_main_app_sees_the_saved_choices_over_the_environment():
    state = {"antigravity": None, "openai_compatible": None}
    gemini_client.use_system_settings(lambda: dict(state))
    try:
        env = Env(antigravity_enabled=False, antigravity_model="env-model", openai_compatible_enabled=True,
                  openai_compatible_label="環境のLLM", openai_compatible_base_url="https://env.example.com",
                  openai_compatible_model="env", openai_compatible_api_key=SecretStr("env-key"))
        assert gemini_client.effective(env).openai_compatible_label == "環境のLLM"
        state["antigravity"] = saved(system_llm.ANTIGRAVITY, ANTIGRAVITY)
        state["openai_compatible"] = saved(system_llm.OPENAI_COMPATIBLE, {"enabled": False})
        gemini_client.refresh_system_settings()
        current = gemini_client.effective(env)
        assert current.antigravity_enabled is True and current.antigravity_model == "gemini-3.8-flash"
        assert current.openai_compatible_enabled is False
    finally:
        gemini_client.use_system_settings(None)


def run_effective(values):
    from backend.worker.controller import Provisioner
    provisioner = Provisioner(controller_settings())

    async def kube(method, resource, name="", body=None, query="", text=False):
        for suffix, data in values.items():
            if name.endswith(suffix):
                return {"data": data}
        return None
    provisioner.kube = kube
    return asyncio.run(provisioner.effective())


def test_agents_get_the_saved_antigravity_and_openai_compatible():
    from backend.worker.controller import SystemAntigravityInput, SystemOpenAICompatibleInput, resources
    antigravity = SystemAntigravityInput(**system_llm.agent_payload(
        system_llm.ANTIGRAVITY, saved(system_llm.ANTIGRAVITY, ANTIGRAVITY), KEY))
    compatible = SystemOpenAICompatibleInput(**system_llm.agent_payload(
        system_llm.OPENAI_COMPATIBLE, saved(system_llm.OPENAI_COMPATIBLE, COMPATIBLE), KEY))
    assert antigravity.api_key.get_secret_value() == "AIza-antigravity-only"
    assert compatible.api_key.get_secret_value() == "sk-compatible-only"

    def stored(payload):
        data = {k: str(v).lower() if isinstance(v, bool) else str(v)
                for k, v in payload.model_dump(exclude={"api_key"}).items()}
        return {**data, "has_key": "true"}
    settings, _ = run_effective({"system-antigravity": stored(antigravity),
                                 "system-openai-compatible": stored(compatible)})
    assert settings.antigravity_enabled and settings.antigravity_max_total_tokens == 60000
    assert settings.openai_compatible_enabled and settings.openai_compatible_model == "qwen-coder"
    pod = resources(UUID(int=1), settings, "shared")["pods"]["spec"]
    env = {item["name"]: item for item in pod["containers"][0]["env"]}
    # Antigravity 専用のキーは別の変数で渡す。Gemini の経路のキーと取り合わない。
    assert env["AGENT_ANTIGRAVITY_API_KEY"]["valueFrom"]["secretKeyRef"]["name"] == "koyorina-system-antigravity"
    assert env["AGENT_OPENAI_COMPATIBLE_API_KEY"]["valueFrom"]["secretKeyRef"]["name"] == \
        "koyorina-system-openai-compatible"
    assert env["AGENT_OPENAI_COMPATIBLE_BASE_URL"]["value"] == "https://llm.example.com/v1"
    assert all("value" not in item or "sk-" not in item["value"] for item in env.values())


def test_the_controller_can_turn_them_off():
    from backend.worker.controller import resources
    settings, _ = run_effective({"system-openai-compatible": {"enabled": "false"}})
    assert settings.openai_compatible_enabled is False
    env = {item["name"] for item in resources(UUID(int=1), settings, "shared")["pods"]["spec"]["containers"][0]["env"]}
    assert "AGENT_OPENAI_COMPATIBLE_ENABLED" not in env


def test_saving_is_audited_sent_and_never_returns_the_key(context, monkeypatch):
    from backend.tests.test_api import login
    client, _ = context
    client.app.state.settings = client.app.state.settings.model_copy(
        update={"codex_controller_url": "http://127.0.0.1:8091", "tenant_secret_key": SecretStr(KEY)})
    sent = []

    async def controller_system(settings, method, path, body=None, timeout=420):
        sent.append((path, body))
        return {}
    monkeypatch.setattr("backend.api.system_settings.controller_system", controller_system)
    login(client)
    response = client.put("/api/system/llm/openai-compatible", json=COMPATIBLE)
    assert response.status_code == 200, response.text
    assert "sk-compatible" not in response.text and response.json()["api_key_configured"]
    assert sent[-1][0] == "/settings/openai-compatible" and sent[-1][1]["api_key"] == "sk-compatible-only"
    rejected = client.put("/api/system/llm/openai-compatible", json={**COMPATIBLE, "base_url": "ftp://x"})
    assert rejected.status_code == 422 and "接続先のURL" in rejected.json()["error"]
    assert client.put("/api/system/llm/unknown", json={"enabled": False}).status_code == 404


def test_only_administrators_change_them(context):
    from backend.tests.test_api import login
    client, _ = context
    login(client, "bob@example.com")
    assert client.get("/api/system/llm/antigravity").status_code == 403
    assert client.put("/api/system/llm/antigravity", json={"enabled": False}).status_code == 403


from backend.tests.test_api import context  # noqa: E402,F401


def test_changing_llm_settings_replaces_idle_agents_but_waits_for_busy_ones():
    """設定を変えても、動いている Pod は古い設定のまま。使っていなければすぐ入れ替える。"""
    from backend.worker.controller import LLM_ANNOTATION, Provisioner, resources
    provisioner = Provisioner(controller_settings(idle_minutes=0))
    tenant = str(UUID(int=9))
    idle_user, busy_user = UUID(int=1), UUID(int=2)
    # 保存前の設定で作られた Pod。指紋は作ったときの設定のもの。
    stale = resources(idle_user, provisioner.settings, "shared", tenant=tenant)["pods"]["metadata"][
        "annotations"][LLM_ANNOTATION]
    pods = [{"metadata": {"name": f"pod-{user.int}", "annotations": {LLM_ANNOTATION: stale},
                          "labels": {"forge-user": str(user), "forge-route": "shared", "forge-tenant": tenant}},
             "status": {"phase": "Running", "conditions": [{"type": "Ready", "status": "True"}]}}
            for user in (idle_user, busy_user)]
    saved_openai = {"enabled": "true", "base_url": "https://llm.example.com/v1", "model": "qwen", "has_key": "true"}
    removed = []

    async def kube(method, resource, name="", body=None, query="", text=False):
        if resource == "configmaps":
            return {"data": saved_openai} if name.endswith("system-openai-compatible") else None
        return {"items": pods} if resource == "pods" and query else None

    async def relay(user, method, path, **kwargs):
        return {"busy": user == busy_user, "idle_seconds": 5}

    async def remove(name, **kwargs):
        removed.append(name)
    provisioner.kube, provisioner.relay, provisioner.remove_worker_endpoint = kube, relay, remove
    asyncio.run(provisioner.reap_idle())
    # 使っていない方だけ入れ替える。生成中の方は、終わった次の見回りで入れ替わる。
    assert removed == ["pod-1"]


CLAUDE = {"enabled": True, "backend": "api_key", "model": "claude-sonnet-5", "api_key": "sk-ant-test-only"}


def test_claude_reaches_the_agents_with_its_key_in_a_secret():
    from backend.worker.controller import SystemClaudeInput, resources, worker_route
    value = saved(system_llm.CLAUDE, CLAUDE)
    assert "sk-ant" not in json.dumps(value)
    payload = SystemClaudeInput(**system_llm.agent_payload(system_llm.CLAUDE, value, KEY))
    data = {k: str(v).lower() if isinstance(v, bool) else str(v)
            for k, v in payload.model_dump(exclude={"api_key"}).items()}
    settings, _ = run_effective({"system-claude": {**data, "has_key": "true"}})
    assert worker_route(settings, "claude") == "claude"
    env = {item["name"]: item for item in resources(UUID(int=1), settings, "shared")["pods"]["spec"]["containers"][0]["env"]}
    assert env["AGENT_CLAUDE_MODEL"]["value"] == "claude-sonnet-5"
    assert env["AGENT_CLAUDE_API_KEY"]["valueFrom"]["secretKeyRef"]["name"].endswith("system-claude")
    assert all("sk-ant" not in str(item.get("value", "")) for item in env.values())


def test_claude_on_vertex_needs_a_project_and_no_key():
    with pytest.raises(ValidationError, match="プロジェクト"):
        system_llm.ClaudeInput(enabled=True, backend="vertex", model="claude-sonnet-5")
    value = saved(system_llm.CLAUDE, {"enabled": True, "backend": "vertex", "model": "claude-opus-5-5",
                                      "gcp_project": "claude-project-01", "location": "global"})
    assert value["api_key_encrypted"] is None
