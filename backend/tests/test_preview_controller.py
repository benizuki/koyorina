"""共有環境のプレビュー実行基盤。Kubernetes APIは呼ばず、要求内容だけを確認する。"""
import asyncio
from uuid import uuid4
import pytest
from fastapi import HTTPException
from pydantic import SecretStr, ValidationError
from backend.config.settings import Settings
from backend.core.preview_backend import ControllerBackend, DockerBackend, backend
from backend.domain.preview import forward_secret, resource_name, service_target
from backend.tests.test_codex_generation import BUNDLE
from backend.worker.preview_controller import ControllerSettings, LaunchInput, Provisioner, manifests

IMAGE = "registry.example.com/koyorina-preview-runtime@sha256:" + "a" * 64
PROJECT = "49913f18-b465-42da-9c21-e1f0a2dd103e"
TENANT = "11111111-1111-4111-8111-111111111111"


def controller_settings(tmp_path):
    return ControllerSettings(token="x" * 40, runtime_image=IMAGE, root=tmp_path / "previews")


def launch(files=None):
    return LaunchInput(job_id="a5811e7e-a100-41d3-9100-0412e340ca68", files=files,
                       app_origin="https://koyorina.example.com", forward_secret="s" * 40,
                       google_client_id="c", admin_email="a@example.com")


class FakeKube:
    def __init__(self, provisioner, deployments=(), pod_status=None, logs=None):
        self.calls = []
        self.deployments = dict(deployments)
        self.pod_status = pod_status or {}
        self.logs = logs or {}
        provisioner.kube = self.kube

    async def kube(self, method, resource, name="", body=None, group="api/v1", query="", text=False):
        self.calls.append((method, resource, name, query))
        if method == "GET" and resource == "deployments" and name:
            return self.deployments.get(name)
        if method == "GET" and resource == "deployments":
            return {"items": [{"metadata": {"name": key}} for key in self.deployments]}
        if method == "GET" and resource == "pods" and name.endswith("/log"):
            if self.logs:
                return self.logs.get("previous" if "previous=true" in query else "current", "")
            return "[preview] 起動しました。\n"
        if method == "GET" and resource == "pods":
            return {"items": [{"metadata": {"name": "preview-pod-1"}, "status": self.pod_status}]}
        if method == "PATCH" and resource == "deployments":
            self.deployments[name] = {"status": {"readyReplicas": 1}}
        return {}


def test_manifests_are_fixed_and_least_privilege(tmp_path):
    shapes = manifests(PROJECT, controller_settings(tmp_path), {"APP_ORIGIN": "https://forge.test"},
                       "2026-09-12T05:00:00+00:00")
    deployment = shapes["deployments"]
    pod = deployment["spec"]["template"]["spec"]
    container = pod["containers"][0]
    assert deployment["metadata"]["name"] == resource_name(PROJECT) == "preview-" + PROJECT.replace("-", "")
    assert deployment["metadata"]["namespace"] == "koyorina-preview"
    assert container["image"] == IMAGE
    assert pod["automountServiceAccountToken"] is False
    assert pod["securityContext"]["runAsNonRoot"] is True
    assert container["securityContext"]["allowPrivilegeEscalation"] is False
    assert container["securityContext"]["capabilities"]["drop"] == ["ALL"]
    # 共有PVCはRWO。実行Podはcontrollerと同じノードに固定する。
    assert pod["nodeSelector"] == {"kubernetes.io/hostname": "k3s-agent-2"}
    assert deployment["spec"]["strategy"]["type"] == "Recreate"
    mounts = {mount["mountPath"]: mount.get("subPath") for mount in container["volumeMounts"]}
    assert mounts["/workspace"] == f"{PROJECT}/workspace" and mounts["/var/preview"] == f"{PROJECT}/var"
    assert container["env"] == [{"name": "APP_ORIGIN", "value": "https://forge.test"}]
    annotations = deployment["spec"]["template"]["metadata"]["annotations"]
    assert annotations["koyorina/launched-at"] == "2026-09-12T05:00:00+00:00"


def test_start_materializes_applies_and_recreates_pod(tmp_path):
    async def run():
        provisioner = Provisioner(controller_settings(tmp_path))
        kube = FakeKube(provisioner)
        state = await provisioner.start(PROJECT, launch(BUNDLE["files"]))
        workspace = provisioner.paths(PROJECT).workspace
        assert (workspace / "backend" / "main.py").is_file()
        applied = [(method, resource) for method, resource, _, _ in kube.calls if method == "PATCH"]
        assert applied == [("PATCH", "services"), ("PATCH", "deployments")]
        # Pod削除の権限を持たせない。注釈の更新でPodを入れ替える。
        assert not [call for call in kube.calls if call[0] == "DELETE" and call[1] == "pods"]
        assert state["state"] == "running" and state["job_id"] == launch().job_id
        # 2回目はコードを送らずに再起動できる。
        assert (await provisioner.start(PROJECT, launch()))["state"] == "running"
    asyncio.run(run())


def test_start_without_source_and_over_the_limit(tmp_path):
    async def run():
        settings = controller_settings(tmp_path).model_copy(update={"max_running": 1})
        provisioner = Provisioner(settings)
        FakeKube(provisioner, {"preview-other": {"status": {"readyReplicas": 1}}})
        with pytest.raises(HTTPException) as missing:
            await provisioner.start(PROJECT, launch())
        assert missing.value.status_code == 409
        with pytest.raises(HTTPException) as full:
            await provisioner.start(PROJECT, launch(BUNDLE["files"]))
        assert full.value.status_code == 409 and "上限" in full.value.detail
    asyncio.run(run())


def test_preview_controller_rejects_a_project_routed_through_another_tenant(tmp_path):
    async def run():
        provisioner = Provisioner(controller_settings(tmp_path))
        deployment = {"metadata": {"labels": {"koyorina/tenant": TENANT}},
                      "status": {"readyReplicas": 1}}
        FakeKube(provisioner, {resource_name(PROJECT): deployment})
        assert (await provisioner.status(TENANT, PROJECT))["state"] == "running"
        with pytest.raises(HTTPException) as refused:
            await provisioner.status("22222222-2222-4222-8222-222222222222", PROJECT)
        assert refused.value.status_code == 404
    asyncio.run(run())


def test_preview_migration_mounts_only_source_and_target_claims(tmp_path):
    from backend.domain.tenant_storage import preview_claim
    from backend.worker.preview_controller import MigrationInput
    source, target, migration = uuid4(), uuid4(), uuid4()

    async def run():
        provisioner = Provisioner(controller_settings(tmp_path).model_copy(update={"root": None}))
        created = []

        async def kube(method, resource, name="", body=None, **kwargs):
            if method == "GET" and resource == "persistentvolumeclaims":
                return None
            if method == "POST":
                created.append(body)
                return body
            if method == "GET" and resource == "pods":
                return {"status": {"phase": "Succeeded"}}
            return {}

        provisioner.kube = kube
        payload = MigrationInput(migration_id=migration, project_id=PROJECT,
                                 source_tenant_id=source, target_tenant_id=target)
        assert (await provisioner.migrate(payload))["status"] == "copied"
        pod = next(item for item in created if item.get("kind") == "Pod")
        compile(pod["spec"]["containers"][0]["command"][2], "<preview-migration>", "exec")
        claims = {volume["persistentVolumeClaim"]["claimName"]
                  for volume in pod["spec"]["volumes"] if "persistentVolumeClaim" in volume}
        assert claims == {preview_claim(source), preview_claim(target)}
    asyncio.run(run())


def test_preview_storage_measurement_mounts_only_target_read_only(tmp_path):
    from backend.domain.tenant_storage import preview_claim

    async def run():
        provisioner = Provisioner(controller_settings(tmp_path))
        created = []

        async def kube(method, resource, name="", body=None, text=False, **kwargs):
            if method == "GET" and resource == "persistentvolumeclaims":
                return {"spec": {"resources": {"requests": {"storage": "20Gi"}}}}
            if method == "POST":
                created.append(body)
                return body
            if method == "GET" and resource == "pods" and name.endswith("/log"):
                return '{"used_bytes":456,"capacity_bytes":1000,"available_bytes":600}'
            if method == "GET" and resource == "pods":
                return {"status": {"phase": "Succeeded"}}
            return {}

        provisioner.kube = kube
        result = await provisioner.measure_storage(TENANT)
        assert result["used_bytes"] == 456
        pod = created[0]
        volumes = [item for item in pod["spec"]["volumes"] if "persistentVolumeClaim" in item]
        assert volumes == [{"name": "storage", "persistentVolumeClaim": {
            "claimName": preview_claim(TENANT), "readOnly": True}}]
        assert pod["spec"]["containers"][0]["volumeMounts"][0]["readOnly"] is True
    asyncio.run(run())


@pytest.mark.parametrize("status,expected", [
    (None, "stopped"),
    ({"status": {"readyReplicas": 1}}, "running"),
    # 揃っていないだけでは失敗と呼ばない。作った直後は必ずこの形を通るため、
    # ここを失敗にすると、開始した瞬間に「異常終了」と表示してしまう。
    ({"status": {"unavailableReplicas": 1}}, "starting"),
    ({"status": {"unavailableReplicas": 1, "updatedReplicas": 1}}, "starting"),
])
def test_status_reflects_the_deployment(tmp_path, status, expected):
    async def run():
        provisioner = Provisioner(controller_settings(tmp_path))
        FakeKube(provisioner, {resource_name(PROJECT): status} if status else {})
        assert (await provisioner.status(PROJECT))["state"] == expected
    asyncio.run(run())


CRASHING = {"containerStatuses": [{"restartCount": 3,
             "state": {"waiting": {"reason": "CrashLoopBackOff"}}}]}
EXITED = {"containerStatuses": [{"restartCount": 1, "state": {"terminated": {"exitCode": 1}}}]}


@pytest.mark.parametrize("pod_status", [CRASHING, EXITED])
def test_a_pod_that_keeps_failing_is_not_reported_as_starting(tmp_path, pod_status):
    """起動を諦めた状態を「起動中」と返すと、待ち続けて失敗に気付けない。"""
    async def run():
        provisioner = Provisioner(controller_settings(tmp_path))
        deployment = {"status": {"unavailableReplicas": 1, "updatedReplicas": 1}}
        FakeKube(provisioner, {resource_name(PROJECT): deployment}, pod_status=pod_status)
        assert (await provisioner.status(PROJECT))["state"] == "failed"
    asyncio.run(run())


def test_logs_fall_back_to_the_run_that_failed(tmp_path):
    """落ちて起動し直した直後、いまのコンテナには何も出ていない。理由は前回の出力にある。"""
    async def run():
        provisioner = Provisioner(controller_settings(tmp_path))
        kube = FakeKube(provisioner, pod_status=CRASHING,
                        logs={"current": "", "previous": "ModuleNotFoundError: No module named 'httpx'\n"})
        assert "ModuleNotFoundError" in await provisioner.logs(PROJECT)
        assert any("previous=true" in query for _, resource, name, query in kube.calls
                   if resource == "pods" and name.endswith("/log"))
    asyncio.run(run())


def test_stop_and_discard_remove_resources_and_workspace(tmp_path):
    async def run():
        provisioner = Provisioner(controller_settings(tmp_path))
        kube = FakeKube(provisioner)
        await provisioner.start(PROJECT, launch(BUNDLE["files"]))
        base = provisioner.paths(PROJECT).base
        assert base.is_dir()
        await provisioner.discard(PROJECT)
        deleted = {resource for method, resource, _, _ in kube.calls if method == "DELETE"}
        assert {"deployments", "services"} <= deleted
        assert not base.exists()
        assert (await provisioner.logs(PROJECT)).startswith("[preview]")
    asyncio.run(run())


def local(**overrides):
    base = {"_env_file": None, "app_env": "local", "database_url": "postgresql+psycopg://u:p@127.0.0.1:5432/d",
            "app_origin": "http://127.0.0.1:8080"}
    return Settings(**{**base, **overrides})


def production(**overrides):
    base = {"_env_file": None, "app_env": "production",
            "database_url": "postgresql+psycopg://u:p@db:5432/d", "app_origin": "https://forge.test",
            "app_session_secret": "k" * 40, "google_oauth_client_id": "c"}
    return Settings(**{**base, **overrides})


@pytest.mark.parametrize("reported,expected", [
    # docker run --detach の直後は created。ここを exited と同じ扱いにしていたため、
    # 開始を押した瞬間に「異常終了」と表示していた。
    ("created", "starting"),
    ("restarting", "starting"),
    ("running", "running"),
    ("exited", "exited"),
    ("dead", "exited"),
])
def test_docker_status_words_are_not_lumped_together(monkeypatch, reported, expected):
    from backend.core import preview_runtime as runtime

    async def docker(*args, environment=None, timeout=60):
        return 0, reported + "\n", ""

    monkeypatch.setattr(runtime, "docker", docker)
    assert asyncio.run(runtime.container_state(PROJECT)) == expected


def docker_backend(tmp_path, monkeypatch, container, responding, minutes_ago=0):
    """状態ファイルだけ用意し、コンテナの様子は差し替えて確かめる。"""
    from datetime import datetime, timedelta, timezone
    from backend.core import preview_backend as module
    from backend.domain.preview import write_state
    settings = local(preview_enabled=True, preview_root=tmp_path / "previews")
    runner = DockerBackend(settings)
    started = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    write_state(runner.paths(PROJECT), {"job_id": "a5811e7e-a100-41d3-9100-0412e340ca68",
                                        "port": 8081, "updated_at": started.isoformat()})

    async def container_state(_project_id):
        return container

    async def responds(_port):
        return responding

    monkeypatch.setattr(module.runtime, "container_state", container_state)
    monkeypatch.setattr(module.runtime, "responding", responds)
    return asyncio.run(runner.status(PROJECT))


@pytest.mark.parametrize("container,responding,expected", [
    # docker は起動した直後、ほんの一瞬 created を返す。ここを exited と同じ扱いに
    # していたため、開始を押した瞬間に「異常終了」と表示していた。
    ("created", False, "starting"),
    ("restarting", False, "starting"),
    ("running", False, "starting"),
    ("running", True, "running"),
    ("exited", False, "failed"),
    ("none", False, "stopped"),
])
def test_only_a_container_that_really_stopped_counts_as_a_failure(
        tmp_path, monkeypatch, container, responding, expected):
    assert docker_backend(tmp_path, monkeypatch, container, responding)["state"] == expected


def test_waiting_forever_is_not_an_option_either(tmp_path, monkeypatch):
    """応答が無いまま上限を過ぎたら失敗と呼ぶ。でないと永久に「起動中」に見える。"""
    result = docker_backend(tmp_path, monkeypatch, "running", False, minutes_ago=30)
    assert result["state"] == "failed"
    assert "応答しませんでした" in result["message"]


def test_docker_backend_stays_local_only():
    assert isinstance(backend(local(preview_enabled=True)), DockerBackend)
    with pytest.raises(ValidationError):
        production(preview_enabled=True, preview_backend="docker")


def test_controller_backend_requires_a_fixed_service_and_key():
    with pytest.raises(ValidationError):
        production(preview_enabled=True, preview_backend="controller",
                   preview_controller_url="http://attacker.example.com")
    with pytest.raises(ValidationError):
        production(preview_enabled=True, preview_backend="controller",
                   preview_controller_url="http://koyorina-preview-controller.koyorina-preview.svc:8080",
                   preview_controller_token=SecretStr("short"))
    settings = production(preview_enabled=True, preview_backend="controller",
                          preview_controller_url="http://koyorina-preview-controller.koyorina-preview.svc:8080",
                          preview_controller_token=SecretStr("t" * 40))
    assert isinstance(backend(settings), ControllerBackend)
    assert backend(settings).target(PROJECT) == service_target(PROJECT)
    assert service_target(PROJECT).startswith("http://preview-49913f18b465")


def test_controller_backend_sends_fixed_operations():
    settings = production(preview_enabled=True, preview_backend="controller",
                          preview_controller_url="http://koyorina-preview-controller.koyorina-preview.svc:8080",
                          preview_controller_token=SecretStr("t" * 40))
    client = ControllerBackend(settings)
    sent = []

    async def call(method, project_id, tenant_id, path="", body=None, **kwargs):
        sent.append((method, project_id, tenant_id, path, body))
        return {"state": "running"}

    client.call = call

    async def run():
        context = {"app_origin": settings.app_origin, "google_client_id": "c", "admin_email": "a@example.com",
                   "forward_secret": forward_secret(PROJECT, settings.app_session_secret)}
        await client.start(PROJECT, None, "job-1", context, TENANT)
        await client.stop(PROJECT, TENANT)
        await client.discard(PROJECT, TENANT)
    asyncio.run(run())
    assert [(method, path) for method, _, _, path, _ in sent] == [
        ("PUT", ""), ("DELETE", ""), ("DELETE", "/workspace")]
    assert all(tenant == TENANT for _, _, tenant, _, _ in sent)
    body = sent[0][4]
    assert body["forward_secret"] == forward_secret(PROJECT, settings.app_session_secret)
    assert body["files"] is None and body["job_id"] == "job-1"


def test_the_preview_budget_comes_from_settings(tmp_path):
    """クラスタの大きさで変えたい値なので、コードに埋めない。"""
    settings = ControllerSettings(token="x" * 40, runtime_image=IMAGE,
                                  root=tmp_path / "previews", cpu_request="250m", cpu_limit="4",
                                  memory_request="1Gi", memory_limit="6Gi")
    spec = manifests(PROJECT, settings, {}, "2026-01-01T00:00:00Z")
    resources = spec["deployments"]["spec"]["template"]["spec"]["containers"][0]["resources"]
    assert resources == {"requests": {"cpu": "250m", "memory": "1Gi"},
                         "limits": {"cpu": "4", "memory": "6Gi"}}


def test_a_malformed_budget_is_refused():
    """単位を取り違えた値でPodを作ると、起動しない理由が分かりにくくなる。"""
    for bad in ({"memory_limit": "3G"}, {"cpu_limit": "2 cores"}, {"memory_request": "abc"}):
        with pytest.raises(ValidationError):
            ControllerSettings(token="x" * 40, runtime_image=IMAGE, **bad)
