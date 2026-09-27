import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from backend.worker.controller import ControllerSettings, resources

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("gce_generation", ROOT / "setup/gcp/k8s/deploy-generation-gce.py")
deploy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deploy)


def test_gce_worker_isolation():
    config = ControllerSettings(token="x" * 40, vertex_project="test-project",
        image_registry="asia-northeast1-docker.pkg.dev/test-project/koyorina",
        agent_image="asia-northeast1-docker.pkg.dev/test-project/koyorina/koyorina-agent@sha256:" + "a" * 64,
        image_pull_secret="koyorina-generation-pull", agent_toleration=True)
    pod = resources(uuid4(), config, "codex", tenant=str(uuid4()))["pods"]["spec"]
    assert not pod["automountServiceAccountToken"]
    assert pod["tolerations"][0]["value"] == "koyorina-agent"
    assert pod["imagePullSecrets"] == [{"name": "koyorina-generation-pull"}]
    env = {e["name"]: e for e in pod["containers"][0]["env"]}
    # トークンファイル方式は廃止。WIF を設定するまでは鍵ファイルを探し、無くても Pod は起動する。
    assert "APP_FORGE_VERTEX_TOKEN_FILE" not in env
    assert env["GOOGLE_APPLICATION_CREDENTIALS"]["value"] == "/run/vertex/key.json"
    vertex = next(v for v in pod["volumes"] if v["name"] == "vertex")
    assert vertex["secret"] == {"secretName": "koyorina-vertex", "optional": True}


def test_missing_storage_verification_stops_before_cluster_access(monkeypatch):
    monkeypatch.setattr(deploy, "get", lambda *a: pytest.fail("No cluster changes expected"))
    with pytest.raises(deploy.SetupError, match="storage-verified"):
        deploy.deploy(SimpleNamespace(storage_verified=False))


def test_refresh_renews_only_the_image_pull_secret(monkeypatch):
    """Vertex のトークンはもう配らない（token-file 方式は廃止）。更新するのは pull token だけ。"""
    writes = []
    monkeypatch.setattr(deploy, "run", lambda *a: "server-only")
    monkeypatch.setattr(deploy.management, "upsert_secret", lambda *a, **kw: writes.append((a, kw)))
    deploy.refresh(SimpleNamespace(project="test-project", registry_host="region-docker.pkg.dev"))
    assert [write[0][:2] for write in writes] == [("koyorina-generation-pull", "koyorina-codex")]


def test_storage_conflict_stops_without_mutation(monkeypatch):
    config = {"data": {"config.json": json.dumps({"nodePathMap": [
        {"node": "agent", "paths": ["/existing-data"]}]})}}
    monkeypatch.setattr(deploy, "get", lambda *a: config)
    monkeypatch.setattr(deploy, "kube", lambda *a, **kw: pytest.fail("No mutation expected"))
    with pytest.raises(deploy.SetupError, match="storage paths differ"):
        deploy.configure_storage("agent")


def test_the_apparmor_profile_is_only_set_when_a_node_has_one():
    """AppArmorが無いノードで指定すると、逆にPodが起動しない。

    開発・本番とも既定はRocky Linux（AppArmor無し）。Ubuntu系ノード
    （containerd既定がmountを拒む）へ切り替えたときだけ明示で渡す。
    同じコードで両方を扱うため、空なら付けない。
    """
    base = dict(token="x" * 40,
                agent_image="registry.example.com/koyorina-agent@sha256:" + "a" * 64)
    without = resources(uuid4(), ControllerSettings(**base), "codex")
    assert "appArmorProfile" not in without["pods"]["spec"]["securityContext"]

    with_profile = resources(uuid4(), ControllerSettings(
        **base, apparmor_profile="koyorina-codex-bwrap"), "codex")
    security = with_profile["pods"]["spec"]["securityContext"]
    assert security["appArmorProfile"] == {"type": "Localhost",
                                           "localhostProfile": "koyorina-codex-bwrap"}
    # restricted は Unconfined を禁じる。名前で指す形でなければ通らない。
    assert security["appArmorProfile"]["type"] != "Unconfined"
    # seccomp は別物。片方で代用できない（拒否は EPERM と EACCES で分かれる）。
    assert security["seccompProfile"]["type"] == "Localhost"
