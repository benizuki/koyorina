"""Tenant copy and cleanup must work on tainted nodes with private images."""
import asyncio
from uuid import uuid4

import pytest

from backend.domain.tenant_storage import preview_claim
from backend.worker.preview_controller import ControllerSettings, MigrationInput, Provisioner


@pytest.mark.parametrize("cleanup", [False, True])
@pytest.mark.parametrize("gce", [False, True])
def test_preview_migration_pod_placement_and_pull_credentials(cleanup, gce):
    settings = ControllerSettings(
        _env_file=None, app_name="ai-terakoya", token="x" * 40,
        runtime_image="registry.example.com/ai-terakoya-preview-runtime@sha256:" + "a" * 64,
        root=None, node_selector={"workload": "ai-terakoya-agent"},
        agent_toleration=gce, image_pull_secret="ai-terakoya-preview-pull" if gce else "")
    provisioner = Provisioner(settings)
    payload = MigrationInput(migration_id=uuid4(), project_id=uuid4(),
                             source_tenant_id=uuid4(), target_tenant_id=uuid4())
    created = []

    async def kube(method, resource, name="", body=None, **kwargs):
        if method == "POST":
            created.append(body)
            return body
        if method == "GET" and resource == "pods":
            return {"status": {"phase": "Succeeded"}}
        return None

    provisioner.kube = kube
    operation = provisioner.cleanup_migration if cleanup else provisioner.migrate
    assert asyncio.run(operation(payload))["status"] == ("cleaned" if cleanup else "copied")
    spec = next(item["spec"] for item in created if item["kind"] == "Pod")
    assert spec["nodeSelector"] == {"workload": "ai-terakoya-agent"}
    if gce:
        assert spec["tolerations"] == [{"key": "workload", "operator": "Equal",
            "value": "ai-terakoya-agent", "effect": "NoSchedule"}]
        assert spec["imagePullSecrets"] == [{"name": "ai-terakoya-preview-pull"}]
    else:
        assert "tolerations" not in spec
        assert "imagePullSecrets" not in spec
    claims = {v["persistentVolumeClaim"]["claimName"] for v in spec["volumes"]
              if "persistentVolumeClaim" in v}
    expected = {preview_claim(payload.source_tenant_id)}
    if not cleanup:
        expected.add(preview_claim(payload.target_tenant_id))
    assert claims == expected
