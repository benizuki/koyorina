import asyncio
import io
import json
import urllib.error

import pytest

from backend.domain import gemini_probe, tenant_ai
from backend.tests.test_tenant_ai import KEY, VERTEX, controller_settings, row

SUBJECT = "k8s-subject-token-test-only"


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def fake_google(monkeypatch, answers):
    """URL ごとに返す値を決める。呼ばれた順と、送られた本文・ヘッダーを残す。"""
    calls = []

    def urlopen(request, timeout):
        body = request.data.decode() if request.data else ""
        calls.append((request.full_url, dict(request.header_items()), body))
        for fragment, answer in answers.items():
            if fragment in request.full_url:
                if isinstance(answer, Exception):
                    raise answer
                return Response(json.dumps(answer).encode())
        raise AssertionError(request.full_url)

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    return calls


def vertex_env(tmp_path, service_account=""):
    saved = row(**VERTEX, wif_service_account=service_account)
    plain, _, _ = tenant_ai.environment(saved, KEY)
    config = tenant_ai.credential_config(saved)
    token = tmp_path / "token"
    token.write_text(SUBJECT)
    config["credential_source"]["file"] = str(token)
    config_file = tmp_path / "credential-config.json"
    config_file.write_text(json.dumps(config))
    return {**plain, "GOOGLE_APPLICATION_CREDENTIALS": str(config_file)}


GENERATED = {"candidates": [{"content": {"parts": [{"text": "OK"}]}}]}


def test_vertex_exchanges_the_kubernetes_token_and_asks_once(tmp_path, monkeypatch):
    calls = fake_google(monkeypatch, {"sts.googleapis.com": {"access_token": "federated-token"},
                                      "aiplatform.googleapis.com": GENERATED})
    result = gemini_probe.run(vertex_env(tmp_path))
    assert result["ok"] is True and result["text"] == "OK" and result["model"] == "gemini-3.5-flash"
    sts, generate = calls
    assert "subject_token=" + SUBJECT in sts[2]
    assert generate[0] == ("https://us-central1-aiplatform.googleapis.com/v1/projects/customer-ai-123/"
                           "locations/us-central1/publishers/google/models/gemini-3.5-flash:generateContent")
    assert generate[1]["Authorization"] == "Bearer federated-token"
    assert json.loads(generate[2])["generationConfig"]["thinkingConfig"] == {"thinkingLevel": "LOW"}
    # 結果にトークンを出さない。
    assert SUBJECT not in json.dumps(result) and "federated-token" not in json.dumps(result)


def test_vertex_impersonates_the_service_account_when_set(tmp_path, monkeypatch):
    calls = fake_google(monkeypatch, {"sts.googleapis.com": {"access_token": "federated-token"},
                                      "iamcredentials.googleapis.com": {"accessToken": "sa-token"},
                                      "aiplatform.googleapis.com": GENERATED})
    env = vertex_env(tmp_path, "vertex-user@customer-ai-123.iam.gserviceaccount.com")
    assert gemini_probe.run(env)["ok"] is True
    assert [url.split("/")[2] for url, _, _ in calls] == [
        "sts.googleapis.com", "iamcredentials.googleapis.com", "us-central1-aiplatform.googleapis.com"]
    assert calls[2][1]["Authorization"] == "Bearer sa-token"


def test_the_failing_step_and_googles_reason_are_reported(tmp_path, monkeypatch):
    denied = urllib.error.HTTPError("https://aiplatform", 403, "Forbidden", {},
                                    io.BytesIO(json.dumps({"error": {"message": "Permission denied on resource"}}).encode()))
    fake_google(monkeypatch, {"sts.googleapis.com": {"access_token": "federated-token"},
                              "aiplatform.googleapis.com": denied})
    result = gemini_probe.run(vertex_env(tmp_path))
    assert result == {"ok": False, "step": "generate", "status": 403, "message": "Permission denied on resource"}


def test_gemini_api_sends_the_key_only_as_a_header(monkeypatch):
    calls = fake_google(monkeypatch, {"generativelanguage.googleapis.com": GENERATED})
    result = gemini_probe.run({"GOOGLE_GENAI_USE_VERTEXAI": "false", "GEMINI_MODEL": "gemini-3.5-flash",
                               "GEMINI_API_KEY": "AIza-test-only"})
    assert result["ok"] is True
    url, headers, _ = calls[0]
    assert "AIza" not in url and headers["X-goog-api-key"] == "AIza-test-only"
    assert "AIza" not in json.dumps(result)


def test_controller_runs_the_probe_as_the_tenant_and_cleans_up(monkeypatch):
    from backend.worker.preview_controller import ProbeInput, Provisioner, tenant_service_account
    _, _, wif = tenant_ai.environment(row(**VERTEX), KEY)
    provisioner = Provisioner(controller_settings())
    calls = []

    async def kube(method, resource, name="", body=None, group="api/v1", query="", text=False):
        calls.append((method, resource, name, body))
        if method == "GET" and resource == "pods" and name.endswith("/log"):
            return 'GEMINI_PROBE {"ok": true, "step": "generate", "text": "OK", "model": "m"}\n'
        if method == "GET" and resource == "pods":
            return {"status": {"phase": "Succeeded"}}
        return {}

    monkeypatch.setattr(provisioner, "kube", kube)
    payload = ProbeInput(extra_env={"GEMINI_MODEL": "m", "GOOGLE_GENAI_USE_VERTEXAI": "true"}, wif=wif)
    tenant = "00000000-0000-4000-8000-000000000001"
    result = asyncio.run(provisioner.probe_gemini(tenant, payload))
    assert result["ok"] is True and result["text"] == "OK"
    pod = next(body for method, resource, _, body in calls if method == "POST" and resource == "pods")
    spec = pod["spec"]
    assert spec["serviceAccountName"] == tenant_service_account(tenant)
    assert spec["automountServiceAccountToken"] is False and spec["restartPolicy"] == "Never"
    # 生成アプリと同じ通信制限（NetworkPolicy）がかかるラベル。
    assert pod["metadata"]["labels"]["app.kubernetes.io/part-of"] == controller_settings().preview_label
    assert spec["volumes"][0]["projected"]["sources"][0]["serviceAccountToken"]["audience"] == wif["audience"]
    assert spec["containers"][0]["command"][:2] == ["python", "-c"]
    name = pod["metadata"]["name"]
    deleted = {(resource, target) for method, resource, target, _ in calls if method == "DELETE"}
    assert {("pods", name), ("secrets", name), ("configmaps", name)} <= deleted


def test_only_administrators_can_run_the_test(context):  # noqa: F811
    from backend.tests.test_api import login
    client, _ = context
    login(client, "bob@example.com")
    assert client.post("/api/tenants/00000000-0000-4000-8000-000000000001/ai-settings/test", json={}).status_code == 403


def test_the_test_uses_the_saved_settings_and_records_only_the_outcome(preview, monkeypatch):  # noqa: F811
    from sqlalchemy import select
    from backend.core.db import Audit, Project
    from backend.tests.test_preview import generated_job
    from backend.tests.test_tenant_ai_api import GEMINI, allow_secrets
    client, sessions, calls, state = preview
    project_id, _ = generated_job(sessions, client)
    allow_secrets(client)
    with sessions() as db:
        tenant_id = db.get(Project, project_id).tenant_id
    path = f"/api/tenants/{tenant_id}/ai-settings"
    assert client.post(path + "/test", json={}).status_code == 409          # まだ保存していない
    client.put(path, json=GEMINI)
    seen = []
    monkeypatch.setattr(gemini_probe, "run", lambda env: seen.append(env) or
                        {"ok": True, "step": "generate", "text": "OK", "model": env["GEMINI_MODEL"]})
    result = client.post(path + "/test", json={}).json()
    assert result["ok"] is True and seen[0]["GEMINI_API_KEY"] == GEMINI["api_key"]
    with sessions() as db:
        audit = db.scalar(select(Audit).where(Audit.action == "tenant.ai_tested", Audit.resource_id == tenant_id))
        assert audit.detail == "ok=True; step=generate"


from backend.tests.test_api import context  # noqa: E402,F401
from backend.tests.test_preview import preview  # noqa: E402,F401
