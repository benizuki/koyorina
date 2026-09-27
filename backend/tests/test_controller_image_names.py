"""Renamed releases must retain exact registry/name/digest restrictions."""
import os
import unittest
from unittest.mock import patch
from uuid import uuid4
import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from pydantic import ValidationError
from backend.worker.controller import ControllerSettings as GenerationSettings
from backend.worker.preview_controller import ControllerSettings as PreviewSettings
from backend.worker.controller import resources
from backend.worker.preview_controller import manifests
from backend.worker.controller import Provisioner as GenerationProvisioner
from backend.worker.preview_controller import Provisioner as PreviewProvisioner
from backend.domain.tenant_storage import storage_measurement_pod
from backend.core.preview_backend import ControllerBackend


class ControllerImageNameTests(unittest.TestCase):
    cases = ((GenerationSettings, "agent_image", "agent", "CONTROLLER_"),
             (PreviewSettings, "runtime_image", "preview-runtime", "PREVIEW_CONTROLLER_"))
    registry = "asia-northeast1-docker.pkg.dev/example-project/ai-terakoya"

    def setUp(self):
        environment = patch.dict(os.environ, {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)

    def test_default_and_renamed_images(self):
        for cls, field, suffix, _ in self.cases:
            for name in ("koyorina", "ai-terakoya"):
                with self.subTest(controller=cls.__module__, name=name):
                    kwargs = {} if name == "koyorina" else {"app_name": name}
                    image = f"{self.registry}/{name}-{suffix}@sha256:" + "a" * 64
                    settings = cls(token="t" * 40, image_registry=self.registry,
                                   **kwargs, **{field: image})
                    self.assertEqual(getattr(settings, field), image)

    def test_renamed_workload_tolerations(self):
        for cls, field, suffix, _ in self.cases:
            for enabled in (True, False):
                with self.subTest(controller=cls.__module__, enabled=enabled):
                    settings = cls(token="t" * 40, app_name="ai-terakoya",
                        image_registry=self.registry, agent_toleration=enabled,
                        node="", node_selector={"workload": "ai-terakoya-agent"},
                        storage_access_mode="ReadWriteMany",
                        **{field: f"{self.registry}/ai-terakoya-{suffix}@sha256:" + "a" * 64})
                    if cls is GenerationSettings:
                        spec = resources(uuid4(), settings)["pods"]["spec"]
                    else:
                        spec = manifests(uuid4(), str(uuid4()), settings, environment={})["deployments"]["spec"]["template"]["spec"]
                    self.assertEqual(spec["nodeSelector"]["workload"], "ai-terakoya-agent")
                    self.assertEqual(spec.get("tolerations", []),
                        [{"key": "workload", "operator": "Equal", "value": "ai-terakoya-agent",
                          "effect": "NoSchedule"}] if enabled else [])

    def test_environment_names(self):
        for cls, field, suffix, prefix in self.cases:
            with self.subTest(controller=cls.__module__), patch.dict(os.environ, {
                prefix + "APP_NAME": "ai-terakoya",
                prefix + "TOKEN": "t" * 40,
                prefix + "IMAGE_REGISTRY": self.registry,
                prefix + field.upper(): f"{self.registry}/ai-terakoya-{suffix}@sha256:" + "b" * 64,
            }):
                self.assertEqual(cls().app_name, "ai-terakoya")

    def test_labels_and_queries_follow_release(self):
        async def check(name):
            generation = GenerationSettings(token="t" * 40, app_name=name,
                agent_image=f"registry.example.com/{name}-agent@sha256:" + "a" * 64)
            user = uuid4()
            templates = Path(__file__).resolve().parents[2] / "setup/helm/koyorina/templates"
            generation_policy = (templates / "codex-controller.yaml").read_text(encoding="utf-8").replace("{{ .Release.Name }}", name)
            preview_policy = (templates / "preview.yaml").read_text(encoding="utf-8").replace("{{ .Release.Name }}", name)
            self.assertIn(f"matchLabels: {{app: {generation.agent_label}}}", generation_policy)
            for auth_only in (False, True):
                documents = resources(user, generation, auth_only=auth_only)
                self.assertEqual(documents["pods"]["metadata"]["labels"]["app"], f"{name}-codex-agent")
                if "services" in documents:
                    self.assertEqual(documents["services"]["spec"]["selector"]["app"], f"{name}-codex-agent")
            provisioner = GenerationProvisioner(generation)
            provisioner.kube = AsyncMock(return_value={"items": []})
            await provisioner.runtime_status(user)
            await provisioner.reap_orphan_services()
            await provisioner.reap_idle()
            # 一覧の問い合わせだけを見る。見回りは設定（ConfigMap）も名前で読む。
            calls = [call for call in provisioner.kube.call_args_list if "query" in call.kwargs]
            self.assertGreaterEqual(len(calls), 4)
            for call in calls:
                self.assertIn(f"app={name}-codex-agent", call.kwargs["query"])
            preview = PreviewSettings(token="t" * 40, app_name=name,
                runtime_image=f"registry.example.com/{name}-preview-runtime@sha256:" + "a" * 64)
            self.assertIn(f"matchLabels: {{app.kubernetes.io/part-of: {preview.preview_label}}}", preview_policy)
            self.assertIn(f".{name}-preview.svc:", ControllerBackend(SimpleNamespace(app_name=name)).target(uuid4()))
            documents = manifests(uuid4(), str(uuid4()), preview, environment={})
            deployment = documents["deployments"]
            for labels in (deployment["metadata"]["labels"], deployment["spec"]["selector"]["matchLabels"],
                           deployment["spec"]["template"]["metadata"]["labels"]):
                self.assertEqual(labels["app.kubernetes.io/part-of"], f"{name}-preview")
            provisioner = PreviewProvisioner(preview)
            provisioner.kube = AsyncMock(return_value={"items": []})
            await provisioner.running()
            self.assertEqual(provisioner.kube.call_args.kwargs["query"],
                             f"?labelSelector=app.kubernetes.io/part-of={name}-preview")
            measurement = storage_measurement_pod("measure", f"{name}-codex", "existing-claim",
                generation.agent_image, {}, toleration=True, app_name=name)
            self.assertEqual(measurement["spec"]["tolerations"][0]["value"], f"{name}-agent")
        for name in ("koyorina", "ai-terakoya", "another-app"):
            with self.subTest(name=name):
                asyncio.run(check(name))

    def test_vertex_secret_follows_app_name_and_auth_workers_do_not_mount_it(self):
        for name in ("koyorina", "ai-terakoya"):
            with self.subTest(name=name):
                settings = GenerationSettings(token="t" * 40, app_name=name,
                    image_registry=self.registry,
                    agent_image=f"{self.registry}/{name}-agent@sha256:" + "a" * 64,
                    vertex_project="example-project")
                volumes = resources(uuid4(), settings)["pods"]["spec"]["volumes"]
                vertex = next(v for v in volumes if v["name"] == "vertex")
                self.assertEqual(vertex["secret"]["secretName"], f"{name}-vertex")
                auth_volumes = resources(uuid4(), settings, auth_only=True)["pods"]["spec"]["volumes"]
                self.assertFalse(any(v["name"] == "vertex" for v in auth_volumes))

    def test_other_names_registries_and_unpinned_images_are_rejected(self):
        for cls, field, suffix, _ in self.cases:
            reference = f"{self.registry}/ai-terakoya-{suffix}"
            for image in (
                reference + ":latest", reference + "@sha256:" + "a" * 63,
                reference + "@sha256:" + "g" * 64,
                reference + "@sha256:" + "a" * 64 + "\n",
                f"{self.registry}/koyorina-{suffix}@sha256:" + "a" * 64,
                f"other.example/ai-terakoya-{suffix}@sha256:" + "a" * 64,
                reference.replace("asia-northeast1-docker.pkg.dev", "asia-northeast1-dockerXpkgXdev")
                + "@sha256:" + "a" * 64,
            ):
                with self.subTest(controller=cls.__module__, image=image), self.assertRaises(ValidationError):
                    cls(token="t" * 40, app_name="ai-terakoya", image_registry=self.registry,
                        **{field: image})

    def test_invalid_app_names_are_rejected(self):
        for cls, field, suffix, _ in self.cases:
            for name in ("", "../other", "a.b", "a-", "A", "a" * 45):
                with self.subTest(controller=cls.__module__, name=name), self.assertRaises(ValidationError):
                    cls(token="t" * 40, app_name=name, image_registry=self.registry,
                        **{field: f"{self.registry}/{name}-{suffix}@sha256:" + "a" * 64})
