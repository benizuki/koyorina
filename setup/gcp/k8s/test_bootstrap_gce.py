"""Offline tests: no cloud or Kubernetes calls."""
import base64
import importlib.util
from pathlib import Path
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("gce_bootstrap", Path(__file__).with_name("bootstrap-gce.py"))
bootstrap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bootstrap)


class BootstrapTests(unittest.TestCase):
    def test_runtime_preserves_credentials_and_resource_version(self):
        original = bootstrap.runtime_document(None, {"DATABASE_URL": "original"})
        original["metadata"]["resourceVersion"] = "17"
        original["metadata"]["annotations"] = {"kubectl.kubernetes.io/last-applied-configuration": "old"}
        updated = bootstrap.runtime_document(original, {"APP_ENV": "production"})
        self.assertEqual(updated["data"]["APP_SESSION_SECRET"], original["data"]["APP_SESSION_SECRET"])
        self.assertEqual(updated["data"]["DATABASE_URL"], original["data"]["DATABASE_URL"])
        self.assertEqual(updated["metadata"]["resourceVersion"], "17")
        self.assertEqual(updated["metadata"]["annotations"], {})
        self.assertNotIn("APP_ENV", original["data"])

    def test_conflicting_credentials_fail_without_overwrite(self):
        original = bootstrap.runtime_document(None, {"DATABASE_URL": "original"})
        with self.assertRaises(bootstrap.SetupError) as caught:
            bootstrap.runtime_document(original, {"DATABASE_URL": "sensitive-replacement"})
        self.assertNotIn("sensitive-replacement", str(caught.exception))
        self.assertEqual(base64.b64decode(original["data"]["DATABASE_URL"]), b"original")

    def test_cloud_sql_encryption(self):
        url = "postgresql+psycopg://forge:password@10.0.0.3:5432/forge"
        self.assertEqual(bootstrap.database_url(url), url + "?sslmode=require")
        self.assertIn("sslmode=verify-full", bootstrap.database_url(url + "?sslmode=verify-full"))
        with self.assertRaises(bootstrap.SetupError):
            bootstrap.database_url(url + "?sslmode=disable")

    def test_job_is_independent_of_management_and_vertex(self):
        pod = bootstrap.job_document("image:latest", "server")["spec"]["template"]["spec"]
        container = pod["containers"][0]
        self.assertEqual(container["imagePullPolicy"], "Always")
        self.assertFalse(pod["automountServiceAccountToken"])
        self.assertNotIn("serviceAccountName", pod)
        self.assertEqual(container["envFrom"], [{"secretRef": {"name": "koyorina-runtime"}}])
        self.assertEqual(pod["volumes"], [{"name": "tmp", "emptyDir": {"sizeLimit": "64Mi"}}])
        compile(container["args"][0], "job", "exec")

    def test_cli_failure_does_not_expose_captured_output(self):
        response = subprocess.CompletedProcess(["gcloud"], 1, "private-url", "private-token")
        with patch.object(bootstrap.subprocess, "run", return_value=response):
            with self.assertRaises(bootstrap.SetupError) as caught:
                bootstrap.run(["gcloud", "secrets", "versions", "access", "latest"])
        self.assertNotIn("private-", str(caught.exception))

    def test_completed_job_does_not_fetch_token_or_rerun(self):
        node = {"metadata": {"labels": {"node-role.kubernetes.io/control-plane": "true"}},
                "status": {"conditions": [{"type": "Ready", "status": "True"}]}}
        runtime = {"data": dict.fromkeys(["DATABASE_URL", "APP_SESSION_SECRET", "GOOGLE_OAUTH_CLIENT_ID",
                                         "BOOTSTRAP_ADMIN_EMAIL", "APP_ORIGIN", "APP_ENV"], "encoded")}
        args = SimpleNamespace(project="example-project-prod", server_node="server", app_name="koyorina",
            image="asia-northeast1-docker.pkg.dev/example-project-prod/koyorina/koyorina:latest")
        with patch.object(bootstrap, "get", side_effect=[node, runtime, {"status": {"succeeded": 1}}]), patch.object(bootstrap, "run") as run, patch("builtins.print"):
            bootstrap.bootstrap(args)
        run.assert_not_called()

    def test_failed_migration_does_not_run_admin_bootstrap_or_leak_logs(self):
        source = bootstrap.job_document("image:latest", "server")["spec"]["template"]["spec"]["containers"][0]["args"][0]
        response = subprocess.CompletedProcess(["alembic"], 1, b"private-url", b"private-password")
        with patch("subprocess.run", return_value=response) as run, patch("builtins.print") as output:
            with self.assertRaises(SystemExit):
                exec(compile(source, "job", "exec"), {})
        self.assertEqual(run.call_count, 1)
        self.assertNotIn("private-", str(output.call_args_list))


REGISTRY = "asia-northeast1-docker.pkg.dev/example-project/koyorina"


class ImageTagTests(unittest.TestCase):
    """values.yamlにはタグを書き、クラスタへはdigestを渡す。"""

    def values(self, text):
        path = Path(self.enterContext(__import__("tempfile").TemporaryDirectory())) / "values.yaml"
        path.write_text(text)
        return path

    def test_the_tag_resolves_to_a_digest_for_each_image(self):
        path = self.values(f"domain: a.example\nregistry: {REGISTRY}\nimageTag: \"v0.1.0-dev\"  # 手で更新\n")
        digests = iter("sha256:" + c * 64 for c in "abc")
        with patch.object(bootstrap, "run", side_effect=lambda cmd: next(digests) + "\n") as run, patch("builtins.print"):
            overrides = bootstrap.resolve_image_tag("koyorina", path)
        self.assertEqual([call.args[0][5] for call in run.call_args_list],
                         [f"{REGISTRY}/koyorina:v0.1.0-dev", f"{REGISTRY}/koyorina-agent:v0.1.0-dev",
                          f"{REGISTRY}/koyorina-preview-runtime:v0.1.0-dev"])
        self.assertEqual(overrides, ["--set", "images.app.digest=sha256:" + "a" * 64,
                                     "--set", "images.agent.digest=sha256:" + "b" * 64,
                                     "--set", "images.previewRuntime.digest=sha256:" + "c" * 64])

    def test_without_a_tag_the_digests_in_values_are_used_as_is(self):
        path = self.values(f"registry: {REGISTRY}\nimageTag: \"\"\nimages:\n  app:\n    digest: sha256:x\n")
        with patch.object(bootstrap, "run") as run:
            self.assertEqual(bootstrap.resolve_image_tag("koyorina", path), [])
        run.assert_not_called()

    def test_a_missing_tag_stops_before_helm(self):
        path = self.values(f"registry: {REGISTRY}\nimageTag: v9\n")
        with patch.object(bootstrap, "run", side_effect=bootstrap.SetupError("x")):
            with self.assertRaises(bootstrap.SetupError) as caught:
                bootstrap.resolve_image_tag("koyorina", path)
        self.assertIn("koyorina:v9", str(caught.exception))

    def test_a_nested_image_tag_key_is_not_mistaken_for_the_top_level_one(self):
        path = self.values(f"registry: {REGISTRY}\nfoo:\n  imageTag: v1\n")
        with patch.object(bootstrap, "run") as run:
            self.assertEqual(bootstrap.resolve_image_tag("koyorina", path), [])
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()


class MigrationJobTests(unittest.TestCase):
    def test_a_later_migration_can_be_run_again(self):
        """2回目以降の移行を当てられること。

        bootstrap の Job は固定名で、完了済みなら作り直さない（初期管理者の登録を
        繰り返さないため）。そのままでは0013以降を当てる手段が無く、手でJobを消す
        ことになっていた。移行だけを何度でも流せる形を別に持つ。
        """
        document = bootstrap.job_document("registry/koyorina:latest", "server-1",
                                          "koyorina-gce-migrate-", only_migrate=True)
        meta = document["metadata"]
        # 毎回別の名前。固定名だと、前回の完了済みJobが残っていて作れない。
        self.assertEqual(meta.get("generateName"), "koyorina-gce-migrate-")
        self.assertNotIn("name", meta)
        args = document["spec"]["template"]["spec"]["containers"][0]["args"][0]
        self.assertIn("alembic", args)
        # 管理者登録は動かさない。移行だけを当てる。
        self.assertNotIn("backend.bootstrap", args)
        self.assertIn("Migration: OK", args)

    def test_the_first_time_still_registers_the_administrator(self):
        first = bootstrap.job_document("registry/koyorina:latest", "server-1")
        self.assertEqual(first["metadata"]["name"], "koyorina-gce-bootstrap-v1")
        self.assertIn("backend.bootstrap",
                      first["spec"]["template"]["spec"]["containers"][0]["args"][0])


class StandaloneTests(unittest.TestCase):
    """この道具はk8sフォルダ（あるいはこの1ファイル）だけをサーバVMへ運んで動かす。

    環境ファイルが手元に無いので、既定値は空になる。読めないことを失敗にせず、
    引数で渡してもらう形にしてある。
    """

    def args(self, **overrides):
        base = {"image": "asia-northeast1-docker.pkg.dev/example-project-prod/koyorina/koyorina:latest",
                "server_node": "koyorina-server", "project": "example-project-prod"}
        return SimpleNamespace(**{**base, **overrides})

    def test_latest_is_accepted_and_always_pulled(self):
        """latest を指す運用でも、古いイメージのまま流れないこと。"""
        bootstrap.check_image(self.args(), "migrate")
        container = bootstrap.job_document("registry/koyorina:latest", "server-1",
                                           "x-", only_migrate=True)
        container = container["spec"]["template"]["spec"]["containers"][0]
        assert container["imagePullPolicy"] == "Always"

    def test_an_image_from_another_project_is_refused(self):
        """このJobは runtime Secret を丸ごと受け取る。DB接続URLもその中にある。"""
        for bad in ("asia-northeast1-docker.pkg.dev/other/koyorina/koyorina:latest",
                    "docker.io/library/python:3.13",
                    "koyorina:latest"):
            with self.assertRaises(bootstrap.SetupError):
                bootstrap.check_image(self.args(image=bad), "migrate")

    def test_the_project_must_be_given_when_there_is_no_environment_file(self):
        with self.assertRaises(bootstrap.SetupError) as caught:
            bootstrap.check_image(self.args(project=""), "migrate")
        assert "--project" in str(caught.exception)
