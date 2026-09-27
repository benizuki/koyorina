import importlib.util
from pathlib import Path
from types import SimpleNamespace
import pytest
from backend.worker.preview_controller import ControllerSettings, manifests

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("gce_preview", ROOT / "setup/gcp/k8s/deploy-preview-gce.py")
deploy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deploy)
IMAGE = "region-docker.pkg.dev/test-project/repo/koyorina-preview-runtime@sha256:" + "a" * 64


def test_runtime_uses_pull_secret_and_taint_without_mounting_credentials():
    settings = ControllerSettings(token="x"*40, image_registry="region-docker.pkg.dev/test-project/repo",
        runtime_image=IMAGE, node="agent", image_pull_secret="koyorina-preview-pull", agent_toleration=True)
    pod = manifests("49913f18-b465-42da-9c21-e1f0a2dd103e", settings, {}, "now")["deployments"]["spec"]["template"]["spec"]
    assert pod["imagePullSecrets"] == [{"name": "koyorina-preview-pull"}]
    assert pod["tolerations"] == [
        {"key": "workload", "operator": "Equal", "value": "koyorina-agent", "effect": "NoSchedule"}]
    assert pod["automountServiceAccountToken"] is False
    assert all("secret" not in v for v in pod["volumes"])
    assert pod["containers"][0]["env"] == []
    assert pod["nodeSelector"] == {"kubernetes.io/hostname": "agent"}


def test_missing_storage_confirmation_stops_before_changes(monkeypatch):
    monkeypatch.setattr(deploy, "kube", lambda *a, **kw: pytest.fail("No mutations expected"))
    with pytest.raises(deploy.SetupError, match="storage-verified"):
        deploy.deploy(SimpleNamespace(storage_verified=False))


def test_refresh_only_updates_preview_pull_secret(monkeypatch):
    calls=[]
    monkeypatch.setattr(deploy, "run", lambda *a: "fake-pull-token")
    monkeypatch.setattr(deploy.management,"upsert_secret", lambda *a, **kw: calls.append((a,kw)))
    deploy.refresh(SimpleNamespace(project="test-project", image_root="region-docker.pkg.dev/test-project/repo"))
    assert len(calls)==1
    assert calls[0][0][:2] == ("koyorina-preview-pull", "koyorina-preview")
    assert calls[0][0][3] == "kubernetes.io/dockerconfigjson"
