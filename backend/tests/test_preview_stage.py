"""準備Podの配置条件をクラスタへ送る前に検証する。"""
import asyncio
import pytest
from backend.worker.preview_controller import ControllerSettings, Provisioner, manifests

PROJECT = "49913f18-b465-42da-9c21-e1f0a2dd103e"
TENANT = "11111111-1111-4111-8111-111111111111"


@pytest.mark.parametrize("enabled", [True, False])
@pytest.mark.parametrize("app_name", ["koyorina", "custom"])
@pytest.mark.parametrize("pull_secret", ["", "preview-pull"])
def test_stage_uses_runtime_scheduling_and_pull_credentials(enabled, app_name, pull_secret):
    """準備Podにも実行Podの配置条件とprivate registry認証を渡す。"""
    settings = ControllerSettings(
        token="x" * 40, app_name=app_name,
        runtime_image=f"registry.example.com/{app_name}-preview-runtime@sha256:" + "a" * 64,
        agent_toleration=enabled, node_selector={"workload": f"{app_name}-agent"},
        image_pull_secret=pull_secret)
    provisioner = Provisioner(settings)
    created = []

    class PodCaptured(Exception):
        pass

    async def kube(method, resource, name="", body=None, **kwargs):
        if method == "GET" and resource == "persistentvolumeclaims":
            return {"metadata": {"name": name}}
        if method == "POST" and resource == "pods":
            created.append(body)
            # Pod送信までを検証し、クラスタ内のexecには進まない。
            raise PodCaptured
        raise AssertionError((method, resource))

    provisioner.kube = kube
    with pytest.raises(PodCaptured):
        asyncio.run(provisioner.stage(TENANT, PROJECT, [], {}))
    stage = created[0]["spec"]
    runtime = manifests(PROJECT, TENANT, settings, {}, "2026-01-01T00:00:00Z")[
        "deployments"]["spec"]["template"]["spec"]
    assert stage["nodeSelector"] == runtime["nodeSelector"]
    assert stage.get("tolerations", []) == runtime.get("tolerations", [])
    assert stage.get("imagePullSecrets", []) == runtime.get("imagePullSecrets", [])
    if pull_secret:
        assert stage["imagePullSecrets"] == [{"name": pull_secret}]
    else:
        assert "imagePullSecrets" not in stage
    if enabled:
        assert stage["tolerations"] == [{"key": "workload", "operator": "Equal",
                                        "value": f"{app_name}-agent", "effect": "NoSchedule"}]
    else:
        assert "tolerations" not in stage
