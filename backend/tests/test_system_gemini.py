import asyncio
import base64
import json
from uuid import UUID

import pytest
from pydantic import SecretStr, ValidationError

from backend.core import gemini_client, k8s_token, secret_box
from backend.config.settings import Settings
from backend.domain import system_gemini
from backend.tests.test_tenant_ai import KEY

VERTEX = {"backend": "vertex", "gcp_project": "example-project-123", "location": "global",
          "model": "gemini-3.5-flash", "thinking_level": "MEDIUM",
          "wif_project_number": "719531980988", "wif_pool_id": "koyorina-main",
          "wif_provider_id": "koyorina-main-provider"}
GEMINI = {"backend": "gemini_api", "model": "gemini-3.8-flash", "api_key": "AIza-test-only"}
AUDIENCE = ("https://iam.googleapis.com/projects/719531980988/locations/global/"
            "workloadIdentityPools/koyorina-main/providers/koyorina-main-provider")


def saved(values):
    return system_gemini.apply(None, system_gemini.SystemGeminiInput(**values), KEY)


def service_account_dir(tmp_path, sub="system:serviceaccount:koyorina:koyorina-viewer"):
    claims = base64.urlsafe_b64encode(json.dumps({"sub": sub}).encode()).decode().rstrip("=")
    (tmp_path / "token").write_text(f"header.{claims}.signature")
    (tmp_path / "ca.crt").write_text("")
    return tmp_path


@pytest.mark.parametrize("change, message", [
    ({"gcp_project": ""}, "GCPプロジェクトID"),
    ({"wif_pool_id": "x"}, "プールID"),
    ({"model": ""}, "モデル名"),
    ({"wif_provider_id": ""}, "プロバイダーID"),  # Vertex AI は必ず WIF
    ({"wif_service_account": "someone@example.com"}, "サービスアカウント"),
])
def test_settings_are_checked(change, message):
    with pytest.raises(ValidationError, match=message):
        system_gemini.SystemGeminiInput(**{**VERTEX, **change})


def test_only_gemini_api_or_vertex_with_wif_can_be_chosen():
    for backend in ("env", "none"):
        with pytest.raises(ValidationError):
            system_gemini.SystemGeminiInput(**{**VERTEX, "backend": backend})
    with pytest.raises(ValidationError):
        system_gemini.SystemGeminiInput(**{**VERTEX, "wif_enabled": False})
    assert saved(VERTEX)["wif_enabled"] is True and system_gemini.wif(saved(VERTEX)) is not None


def test_until_saved_the_environment_is_used():
    assert system_gemini.overrides(None, KEY) == {} and system_gemini.agent_payload(None, KEY) == {}
    # 以前 WIF 無しで保存した Vertex AI は、環境の鍵ファイルで動き続ける。
    legacy = {**saved(VERTEX), "wif_enabled": False}
    assert system_gemini.wif(legacy) is None and system_gemini.stored(legacy)["backend"] == "vertex"


def test_the_app_can_start_before_gemini_is_configured_in_the_gui():
    settings = Settings(
        _env_file=None, app_env="production",
        database_url="postgresql+psycopg://user:password@database/koyorina",
        app_origin="https://koyorina.example.com", app_session_secret="x" * 32,
        google_oauth_client_id="123-example.apps.googleusercontent.com",
        pdf_extraction_enabled=True, gemini_api_backend="developer", gemini_api_key="",
        vertex_project="", vertex_location="", vertex_model="",
    )
    assert gemini_client.available_in(settings) is False


def test_the_api_key_is_sealed_kept_and_dropped_when_unused():
    value = saved(GEMINI)
    assert value["api_key_encrypted"] and "AIza" not in json.dumps(value)
    kept = system_gemini.apply(value, system_gemini.SystemGeminiInput(backend="gemini_api", model="m"), KEY)
    assert kept["api_key_encrypted"] == value["api_key_encrypted"]
    assert system_gemini.apply(value, system_gemini.SystemGeminiInput(**VERTEX), KEY)["api_key_encrypted"] is None
    with pytest.raises(Exception, match="TENANT_SECRET_KEY"):
        system_gemini.apply(None, system_gemini.SystemGeminiInput(**GEMINI), "")


def test_overrides_for_each_backend():
    vertex = system_gemini.overrides(saved(VERTEX), KEY)
    assert vertex == {"vertex_model": "gemini-3.5-flash", "gemini_thinking_level": "MEDIUM",
                      "gemini_api_backend": "vertex", "vertex_project": "example-project-123", "vertex_location": "global"}
    gemini = system_gemini.overrides(saved(GEMINI), KEY)
    assert gemini["gemini_api_backend"] == "developer" and gemini["gemini_api_key"].get_secret_value() == "AIza-test-only"


def test_agent_payload_carries_wif_only_for_vertex():
    payload = system_gemini.agent_payload(saved(VERTEX), KEY)
    assert payload["backend"] == "vertex" and payload["project"] == "example-project-123"
    assert payload["audience"] == AUDIENCE
    assert json.loads(payload["config"])["credential_source"]["file"] == "/run/vertex/token"
    gemini = system_gemini.agent_payload(saved(GEMINI), KEY)
    assert gemini["backend"] == "developer" and gemini["api_key"] == "AIza-test-only" and "audience" not in gemini


def test_the_service_account_is_read_from_the_mounted_token(tmp_path):
    directory = service_account_dir(tmp_path)
    assert k8s_token.identity(directory) == ("koyorina", "koyorina-viewer")


def test_the_supplier_requests_an_audience_bound_token_and_reuses_it(tmp_path, monkeypatch):
    directory = service_account_dir(tmp_path)
    requests = []

    class Answer:
        def raise_for_status(self):
            pass

        def json(self):
            return {"status": {"token": "audience-bound-token"}}

    monkeypatch.setattr(k8s_token.httpx, "post", lambda url, **kw: requests.append((url, kw["json"])) or Answer())
    monkeypatch.setattr(k8s_token.ssl, "create_default_context", lambda **kwargs: None)
    supplier = k8s_token.KubernetesTokenSupplier(AUDIENCE, directory)
    assert supplier.get_subject_token(None, None) == supplier.get_subject_token(None, None) == "audience-bound-token"
    assert len(requests) == 1
    url, body = requests[0]
    assert url.endswith("/namespaces/koyorina/serviceaccounts/koyorina-viewer/token")
    assert body["spec"] == {"audiences": [AUDIENCE], "expirationSeconds": 3600}


class Env:
    """Settings の代わり。model_copy だけ持てばよい。"""
    def __init__(self, **values):
        base = {"gemini_api_backend": "vertex", "vertex_project": "env-project", "vertex_location": "us-central1",
                "vertex_model": "env-model", "gemini_thinking_level": "", "gemini_api_key": SecretStr(""),
                "tenant_secret_key": SecretStr(KEY)}
        self.__dict__.update({**base, **values})

    def model_copy(self, update):
        return Env(**{**self.__dict__, **update})


def test_the_main_app_uses_the_system_settings_over_the_environment():
    state = {"value": None}
    gemini_client.use_system_settings(lambda: {"gemini": state["value"]})
    try:
        env = Env()
        assert gemini_client.model(env) == "env-model" and gemini_client.thinking_config(env) is None
        state["value"] = saved(VERTEX)
        gemini_client.refresh_system_settings()
        effective = gemini_client.effective(env)
        assert (effective.vertex_project, gemini_client.model(env)) == ("example-project-123", "gemini-3.5-flash")
        assert gemini_client.thinking_config(env).thinking_level.value == "MEDIUM"
        credentials = gemini_client._vertex_credentials()
        assert credentials._audience == "//" + AUDIENCE.removeprefix("https://")
        assert gemini_client._vertex_credentials() is credentials  # トークンも使い回す
        state["value"] = saved(GEMINI)
        gemini_client.refresh_system_settings()
        assert gemini_client.effective(env).gemini_api_backend == "developer"
        assert gemini_client.available(env) is True
        assert gemini_client._vertex_credentials() is None
    finally:
        gemini_client.use_system_settings(None)


def controller_settings(**values):
    from backend.worker.controller import ControllerSettings
    return ControllerSettings(_env_file=None, token="x" * 40,
                              agent_image="registry.example.com/koyorina-agent@sha256:" + "a" * 64,
                              vertex_project="env-project", gemini_api_backend="vertex", **values)


def run_effective(data):
    from backend.worker.controller import Provisioner
    provisioner = Provisioner(controller_settings())

    async def kube(method, resource, name="", body=None, query="", text=False):
        return {"data": data} if data is not None else None
    provisioner.kube = kube
    return asyncio.run(provisioner.effective())


def test_agents_use_the_system_vertex_with_workload_identity():
    from backend.worker.controller import agent_service_account, resources
    payload = system_gemini.agent_payload(saved(VERTEX), KEY)
    settings, wif = run_effective({"backend": "vertex", "model": payload["model"], "project": payload["project"],
                                   "location": payload["location"], "audience": payload["audience"],
                                   "credential-config.json": payload["config"]})
    pod = resources(UUID(int=1), settings, "shared", wif=wif)["pods"]["spec"]
    assert pod["serviceAccountName"] == agent_service_account(settings)
    assert pod["automountServiceAccountToken"] is False
    vertex = next(v for v in pod["volumes"] if v["name"] == "vertex")
    assert "secret" not in vertex and vertex["projected"]["sources"][0]["serviceAccountToken"]["audience"] == AUDIENCE
    env = {item["name"]: item.get("value") for item in pod["containers"][0]["env"]}
    assert env["GOOGLE_CLOUD_PROJECT"] == "example-project-123" and env["AGENT_GEMINI_MODEL"] == "gemini-3.5-flash"
    assert env["GOOGLE_APPLICATION_CREDENTIALS"] == "/run/vertex/credential-config.json"


def test_agents_use_the_system_gemini_api_key_from_its_own_secret():
    from backend.worker.controller import resources
    settings, wif = run_effective({"backend": "developer", "model": "gemini-3.8-flash"})
    pod = resources(UUID(int=1), settings, "shared", wif=wif)["pods"]["spec"]
    key = next(item for item in pod["containers"][0]["env"] if item["name"] == "GEMINI_API_KEY")
    assert key["valueFrom"]["secretKeyRef"]["name"] == "koyorina-system-gemini"
    assert not [v for v in pod["volumes"] if v["name"] == "vertex"]


def test_without_system_settings_agents_keep_the_environment():
    from backend.worker.controller import resources
    settings, wif = run_effective(None)
    pod = resources(UUID(int=1), settings, "shared", wif=wif)["pods"]["spec"]
    assert "serviceAccountName" not in pod
    vertex = next(v for v in pod["volumes"] if v["name"] == "vertex")
    assert vertex["secret"]["secretName"] == "koyorina-vertex"
    env = {item["name"]: item.get("value") for item in pod["containers"][0]["env"]}
    assert env["GOOGLE_CLOUD_PROJECT"] == "env-project"


def test_system_settings_can_enable_gemini_where_the_environment_did_not():
    from backend.worker.controller import worker_route
    settings, _ = run_effective({"backend": "developer", "model": "m"})
    assert worker_route(settings.model_copy(update={"generator": "codex"}), "gemini") == "gemini"


@pytest.mark.parametrize("change", [
    {"credential_source": {"file": "/etc/passwd", "format": {"type": "text"}}},
    {"token_url": "https://attacker.example/token"},
])
def test_the_controller_refuses_a_config_that_points_elsewhere(change):
    from backend.worker.controller import SystemGeminiInput
    payload = system_gemini.agent_payload(saved(VERTEX), KEY)
    config = {**json.loads(payload["config"]), **change}
    with pytest.raises(ValidationError):
        SystemGeminiInput(**{**payload, "config": json.dumps(config)})
    assert SystemGeminiInput().backend == ""  # 空は「環境の設定へ戻す」


def test_only_administrators_change_the_system_settings(context):
    from backend.tests.test_api import login
    client, _ = context
    login(client, "bob@example.com")
    assert client.get("/api/system/gemini").status_code == 403
    assert client.put("/api/system/gemini", json=VERTEX).status_code == 403


def test_saving_is_stored_audited_and_sent_to_the_controller(context, monkeypatch):
    from sqlalchemy import select
    from backend.core.db import Audit, SystemSetting
    from backend.tests.test_api import login
    client, sessions = context
    client.app.state.settings = client.app.state.settings.model_copy(
        update={"codex_controller_url": "http://127.0.0.1:8091", "tenant_secret_key": SecretStr(KEY)})
    sent = []

    async def controller_system(settings, method, path, body=None, timeout=420):
        sent.append((method, path, body))
        return {}

    monkeypatch.setattr("backend.api.system_settings.controller_system", controller_system)
    login(client)
    response = client.put("/api/system/gemini", json=GEMINI)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["backend"] == "gemini_api" and body["api_key_configured"] and body["agents_synced"] is True
    assert "AIza" not in response.text
    assert sent[-1][:2] == ("PUT", "/settings/gemini") and sent[-1][2]["api_key"] == "AIza-test-only"
    with sessions() as db:
        value = db.get(SystemSetting, system_gemini.KEY).value
        assert "AIza" not in json.dumps(value) and secret_box.open_(KEY, value["api_key_encrypted"]) == "AIza-test-only"
        details = [a.detail for a in db.scalars(select(Audit).where(Audit.action == "system.gemini_updated"))]
        assert "backend=gemini_api" in details and all("AIza" not in (d or "") for d in details)
    assert client.put("/api/system/gemini", json={"backend": "env"}).status_code == 422


def test_the_reason_for_rejected_settings_is_shown(context):
    from backend.tests.test_api import login
    client, _ = context
    login(client)
    response = client.put("/api/system/gemini", json={**VERTEX, "wif_pool_id": "x"})
    assert response.status_code == 422 and "プールID" in response.json()["error"]


from backend.tests.test_api import context  # noqa: E402,F401


def test_the_connection_test_keeps_the_client_open_until_it_answers(context, monkeypatch):
    """google-genai の Client は参照が切れると閉じる。続けて書くと、呼ぶ前に閉じられる。"""
    from types import SimpleNamespace
    from backend.tests.test_api import login
    client, _ = context

    class ClosingClient:
        """本物と同じく、参照が無くなったら閉じる。閉じた後に呼ばれたら失敗する。"""
        def __init__(self):
            self.state = {"closed": False}
            state = self.state

            def generate_content(**kwargs):
                if state["closed"]:
                    raise RuntimeError("Cannot send a request, as the client has been closed.")
                return SimpleNamespace(text="OK")
            self.models = SimpleNamespace(generate_content=generate_content)

        def close(self):
            self.state["closed"] = True

        def __del__(self):
            self.close()

    monkeypatch.setattr(gemini_client, "available", lambda settings: True)
    monkeypatch.setattr(gemini_client, "model", lambda settings: "gemini-3.5-flash")
    monkeypatch.setattr(gemini_client, "thinking_config", lambda settings: None)
    monkeypatch.setattr(gemini_client, "client", lambda settings, **kwargs: ClosingClient())
    login(client)
    result = client.post("/api/system/gemini/test", json={}).json()
    assert result["ok"] is True and result["text"] == "OK", result
