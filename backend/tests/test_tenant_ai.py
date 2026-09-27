import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from backend.core import secret_box
from backend.core.db import TenantAiSettings
from backend.domain import tenant_ai

KEY = "test-only-tenant-secret-key-0123456789abcdef"
VERTEX = {"backend": "vertex", "gcp_project": "customer-ai-123", "location": "us-central1",
          "model": "gemini-3.5-flash", "thinking_level": "LOW", "wif_project_number": "123456789012",
          "wif_pool_id": "koyorina-pool", "wif_provider_id": "k3s-cluster"}


def row(**values):
    fields = {"backend": "none", "api_key_encrypted": None, "gcp_project": "", "location": "",
              "model": "", "thinking_level": "", "wif_project_number": "", "wif_pool_id": "",
              "wif_provider_id": "", "wif_service_account": ""}
    return TenantAiSettings(tenant_id="t", **{**fields, **values})


def test_secret_box_round_trip_and_refuses_without_or_with_another_key():
    sealed = secret_box.seal(KEY, "AIza-test-only")
    assert "AIza-test-only" not in sealed
    assert secret_box.open_(KEY, sealed) == "AIza-test-only"
    with pytest.raises(secret_box.SecretBoxUnavailable):
        secret_box.seal("", "value")
    with pytest.raises(secret_box.SecretBoxUnavailable):
        secret_box.open_(KEY.replace("0", "1"), sealed)


@pytest.mark.parametrize("change, message", [
    ({"gcp_project": "Bad_Project"}, "GCPプロジェクトID"),
    ({"location": ""}, "リージョン"),
    ({"wif_project_number": "abc"}, "プールのあるプロジェクトの番号"),
    ({"wif_pool_id": "x"}, "プールID"),
    ({"model": ""}, "モデル名"),
    ({"wif_service_account": "someone@example.com"}, "サービスアカウント"),
])
def test_vertex_settings_are_checked(change, message):
    with pytest.raises(ValidationError, match=message):
        tenant_ai.TenantAiInput(**{**VERTEX, **change})


def test_thinking_level_is_one_of_the_documented_values():
    with pytest.raises(ValidationError):
        tenant_ai.TenantAiInput(**{**VERTEX, "thinking_level": "EXTREME"})


def test_gemini_api_key_is_sealed_kept_and_dropped_when_unused():
    saved = row()
    tenant_ai.apply(saved, tenant_ai.TenantAiInput(backend="gemini_api", model="gemini-3.5-flash",
                                                   api_key="AIza-test-only"), KEY)
    assert saved.api_key_encrypted and "AIza" not in saved.api_key_encrypted
    first = saved.api_key_encrypted
    # 送られてこなければ前の値を残す（画面には返していないので）。
    tenant_ai.apply(saved, tenant_ai.TenantAiInput(backend="gemini_api", model="gemini-3.8-flash"), KEY)
    assert saved.api_key_encrypted == first and saved.model == "gemini-3.8-flash"
    # 使わない方式にしたら持ち続けない。
    tenant_ai.apply(saved, tenant_ai.TenantAiInput(**VERTEX), KEY)
    assert saved.api_key_encrypted is None and saved.gcp_project == "customer-ai-123"


def test_gemini_api_needs_a_key_and_a_sealing_key():
    with pytest.raises(tenant_ai.TenantAiError, match="APIキーを入力"):
        tenant_ai.apply(row(), tenant_ai.TenantAiInput(backend="gemini_api", model="m"), KEY)
    with pytest.raises(tenant_ai.TenantAiError, match="TENANT_SECRET_KEY"):
        tenant_ai.apply(row(), tenant_ai.TenantAiInput(backend="gemini_api", model="m", api_key="k"), "")


def test_visible_never_returns_the_key():
    saved = row(backend="gemini_api", model="m", api_key_encrypted=secret_box.seal(KEY, "AIza-test-only"))
    shown = tenant_ai.visible(saved, KEY)
    assert shown["api_key_configured"] is True and shown["secrets_available"] is True
    assert "AIza-test-only" not in json.dumps(shown) and saved.api_key_encrypted not in json.dumps(shown)


def test_environment_for_gemini_api_keeps_the_key_out_of_plain_values():
    saved = row(backend="gemini_api", model="gemini-3.5-flash", thinking_level="LOW",
                api_key_encrypted=secret_box.seal(KEY, "AIza-test-only"))
    plain, secret, wif = tenant_ai.environment(saved, KEY)
    assert plain == {"GOOGLE_GENAI_USE_VERTEXAI": "false", "GEMINI_MODEL": "gemini-3.5-flash",
                     "GEMINI_THINKING_LEVEL": "LOW"}
    assert secret == {"GEMINI_API_KEY": "AIza-test-only"} and wif is None
    # 鍵が違えば渡さない（アプリ側はGeminiの機能だけが止まる）。
    assert tenant_ai.environment(saved, "")[1] == {}


def test_environment_for_vertex_uses_workload_identity():
    saved = row(**{k: v for k, v in VERTEX.items()})
    plain, secret, wif = tenant_ai.environment(saved, KEY)
    assert plain["GOOGLE_GENAI_USE_VERTEXAI"] == "true"
    assert plain["GOOGLE_CLOUD_PROJECT"] == "customer-ai-123" and plain["GOOGLE_CLOUD_LOCATION"] == "us-central1"
    assert plain["GOOGLE_APPLICATION_CREDENTIALS"] == "/var/run/koyorina/gcp/credential-config.json"
    assert secret == {}
    assert wif["audience"] == ("https://iam.googleapis.com/projects/123456789012/locations/global/"
                               "workloadIdentityPools/koyorina-pool/providers/k3s-cluster")
    config = json.loads(wif["config"])
    assert config["credential_source"]["file"] == "/var/run/koyorina/gcp/token"
    assert "service_account_impersonation_url" not in config


def test_credential_config_is_readable_by_google_auth():
    from google.auth import identity_pool
    for account in ("", "vertex-user@customer-ai-123.iam.gserviceaccount.com"):
        saved = row(**VERTEX, wif_service_account=account)
        credentials = identity_pool.Credentials.from_info(tenant_ai.credential_config(saved))
        assert bool(credentials._service_account_impersonation_url) is bool(account)


def test_no_tenant_gemini_means_nothing_is_added():
    assert tenant_ai.environment(None, KEY) == ({}, {}, None)
    assert tenant_ai.environment(row(), KEY) == ({}, {}, None)
    assert tenant_ai.llm_summary(row()) is None
    assert tenant_ai.llm_summary(row(**VERTEX)) == {"available": True, "backend": "vertex"}


def test_project_values_win_over_the_tenant_defaults():
    tenant = ({"GEMINI_MODEL": "gemini-3.5-flash", "GOOGLE_GENAI_USE_VERTEXAI": "true"},
              {"GEMINI_API_KEY": "tenant-key"}, {"audience": "a", "config": "{}"})
    extra, secret, wif = tenant_ai.layered(tenant, {"GEMINI_MODEL": "gemini-3.8-flash", "OTHER": "x"})
    assert extra == {"GEMINI_MODEL": "gemini-3.8-flash", "GOOGLE_GENAI_USE_VERTEXAI": "true", "OTHER": "x"}
    assert secret == {"GEMINI_API_KEY": "tenant-key"} and wif is not None
    # 自分のキー・自分の資格情報を指定したプロジェクトには、テナントの分を渡さない。
    extra, secret, wif = tenant_ai.layered(tenant, {"GEMINI_API_KEY": "own",
                                                    "GOOGLE_APPLICATION_CREDENTIALS": "/own.json"})
    assert secret == {} and wif is None and extra["GEMINI_API_KEY"] == "own"


def test_the_guide_names_every_variable_the_tenant_provides():
    guide = (Path(tenant_ai.__file__).resolve().parents[1] / "conventions" / "AGENTS.md").read_text()
    for name in tenant_ai.NAMES:
        assert f"`{name}`" in guide, name
    assert "genai.Client()" in guide and "llm.available" in guide


def controller_settings():
    from backend.tests.test_gce_preview import IMAGE
    from backend.worker.preview_controller import ControllerSettings
    return ControllerSettings(token="x" * 40, image_registry="region-docker.pkg.dev/test-project/repo",
                              runtime_image=IMAGE)


PROJECT = "49913f18-b465-42da-9c21-e1f0a2dd103e"
TENANT = "00000000-0000-4000-8000-000000000001"


def pod(**kwargs):
    from backend.worker.preview_controller import manifests
    shapes = manifests(PROJECT, TENANT, controller_settings(), {"GEMINI_MODEL": "gemini-3.5-flash"},
                       "now", **kwargs)
    return shapes["deployments"]["spec"]["template"]["spec"]


def test_controller_passes_the_api_key_by_reference_only():
    spec = pod(secret_names=["GEMINI_API_KEY"])
    env = spec["containers"][0]["env"]
    key = next(item for item in env if item["name"] == "GEMINI_API_KEY")
    assert "value" not in key
    assert key["valueFrom"]["secretKeyRef"] == {"name": "preview-" + PROJECT.replace("-", "") + "-env",
                                                "key": "GEMINI_API_KEY"}


def test_controller_mounts_a_workload_identity_token_for_the_tenant():
    from backend.worker.preview_controller import WorkloadIdentity, tenant_service_account
    saved = row(**VERTEX)
    _, _, wif = tenant_ai.environment(saved, KEY)
    spec = pod(wif=WorkloadIdentity(**wif))
    assert spec["serviceAccountName"] == tenant_service_account(TENANT)
    # Kubernetes API のトークンは載せない。GCP向けの限定トークンだけ。
    assert spec["automountServiceAccountToken"] is False
    volume = next(v for v in spec["volumes"] if v["name"] == "gcp")
    token = volume["projected"]["sources"][0]["serviceAccountToken"]
    assert token["audience"] == wif["audience"] and token["expirationSeconds"] == 3600
    mount = next(m for m in spec["containers"][0]["volumeMounts"] if m["name"] == "gcp")
    assert mount == {"name": "gcp", "mountPath": "/var/run/koyorina/gcp", "readOnly": True}


def test_without_tenant_gemini_the_pod_is_unchanged():
    spec = pod()
    assert "serviceAccountName" not in spec
    assert not [v for v in spec["volumes"] if v["name"] == "gcp"]
    assert all("valueFrom" not in item for item in spec["containers"][0]["env"])


@pytest.mark.parametrize("change", [
    {"credential_source": {"file": "/etc/passwd", "format": {"type": "text"}}},
    {"token_url": "https://attacker.example/token"},
    {"type": "service_account"},
])
def test_controller_refuses_a_credential_config_that_points_elsewhere(change):
    from backend.worker.preview_controller import WorkloadIdentity
    _, _, wif = tenant_ai.environment(row(**VERTEX), KEY)
    config = {**json.loads(wif["config"]), **change}
    with pytest.raises(ValidationError):
        WorkloadIdentity(audience=wif["audience"], config=json.dumps(config))
