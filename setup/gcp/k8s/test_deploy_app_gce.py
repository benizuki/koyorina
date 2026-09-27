"""Offline tests for GCE management deployment; no cloud calls.

生マニフェストの組み立て(build_documents)とdigest解決(resolve_image)は
setup/helm/koyorinaのHelm Chartへ移した。そちらは`helm lint`/`helm template`
で検証する（このファイルの対象外）。ここに残るのは、Chartが作らない
Secretのガード付き作成・pull tokenの更新・Cloud SQL初期化の前提確認だけ。
"""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("gce_deploy", ROOT / "deploy-app-gce.py")
deploy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deploy)


class DeploymentTests(unittest.TestCase):
    def args(self):
        return SimpleNamespace(project="example-project-prod", registry_host="asia-northeast1-docker.pkg.dev",
            server_node="server", chart="/opt/koyorina-setup/helm/koyorina",
            values="/opt/koyorina-setup/helm/koyorina-values.gce.yaml", release="koyorina")

    def test_pair_reuses_existing_credential(self):
        token = "unchanged-service-credential-" * 2
        controller = {"data": {"token": deploy.base64.b64encode(token.encode()).decode()}}
        with patch.object(deploy, "get", side_effect=[controller, None]), patch.object(deploy, "upsert_secret") as write:
            deploy.ensure_pair("ns", "controller", "client", "TOKEN")
        self.assertEqual(write.call_args_list[0].args[2], {"token": token})
        self.assertEqual(write.call_args_list[1].args[2], {"TOKEN": token})

    def test_mismatched_pair_fails_without_mutation(self):
        encoded = lambda value: deploy.base64.b64encode(value.encode()).decode()
        controller = {"data": {"token": encoded("a"*48)}}
        client = {"data": {"TOKEN": encoded("b"*48)}}
        with patch.object(deploy, "get", side_effect=[controller, client]), patch.object(deploy, "upsert_secret") as write:
            with self.assertRaises(deploy.SetupError):
                deploy.ensure_pair("ns", "controller", "client", "TOKEN")
        write.assert_not_called()

    def test_renamed_client_secret_is_written_in_management_namespace(self):
        token = "existing-controller-token-" * 2
        controller = {"data": {"token": deploy.base64.b64encode(token.encode()).decode()}}
        for suffix, key in (("codex", "CODEX_CONTROLLER_TOKEN"),
                            ("preview", "PREVIEW_CONTROLLER_TOKEN")):
            namespace = f"ai-terakoya-{suffix}"
            with self.subTest(suffix=suffix), \
                 patch.object(deploy, "get", side_effect=[controller, None]) as read, \
                 patch.object(deploy, "upsert_secret") as write:
                deploy.ensure_pair(namespace, f"{namespace}-controller",
                                   f"{namespace}-client", key, "ai-terakoya")
                self.assertEqual(read.call_args.args,
                                 ("secret", f"{namespace}-client", "ai-terakoya"))
                self.assertEqual(write.call_args_list[0].args,
                                 (f"{namespace}-controller", namespace, {"token": token}))
                self.assertEqual(write.call_args_list[1].args,
                                 (f"{namespace}-client", "ai-terakoya", {key: token}))

    def test_renamed_timers_target_the_matching_service(self):
        for suffix, installer in (("app", "install_refresh_timer"),
                                  ("generation", "install_timer"),
                                  ("preview", "install_timer")):
            spec = importlib.util.spec_from_file_location("timer_test", ROOT / f"deploy-{suffix}-gce.py")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            args = self.args()
            args.release = "ai-terakoya"
            args.image_root = "asia-northeast1-docker.pkg.dev/example-project-prod/repo"
            args.vertex_service_account = ""
            written = {}
            def capture(path, content):
                written[path.name] = content
            with self.subTest(suffix=suffix), patch.object(Path, "mkdir"), \
                 patch.object(Path, "chmod"), patch.object(Path, "exists", return_value=False), \
                 patch.object(Path, "write_text", capture), \
                 patch.object(module.shutil, "copyfile"), patch.object(module, "run"):
                getattr(module, installer)(args)
            unit = f"ai-terakoya-{'pull' if suffix == 'app' else suffix}-refresh"
            self.assertIn(f"Unit={unit}.service", written[f"{unit}.timer"])
            self.assertIn("--release ai-terakoya", written[f"{unit}.service"])
            self.assertNotIn("{args.release}", "".join(written.values()))

    def test_refresh_passes_credentials_via_stdin_not_argv(self):
        with patch.object(deploy, "run", return_value="sensitive-token"), patch.object(deploy, "get", return_value=None), patch.object(deploy, "kube") as kube, patch("builtins.print"):
            deploy.refresh_pull(self.args())
        self.assertNotIn("sensitive-token", str(kube.call_args.args))
        payload = kube.call_args.kwargs["document"]
        self.assertEqual(payload["type"], "kubernetes.io/dockerconfigjson")
        config = json.loads(deploy.base64.b64decode(payload["data"][".dockerconfigjson"]))
        self.assertEqual(deploy.base64.b64decode(config["auths"][self.args().registry_host]["auth"]),
                         b"oauth2accesstoken:sensitive-token")

    def test_incomplete_bootstrap_stops_before_applying_resources(self):
        values = {"DATABASE_URL": "hidden", "APP_SESSION_SECRET": "s"*48,
                  "GOOGLE_OAUTH_CLIENT_ID": "client", "BOOTSTRAP_ADMIN_EMAIL": "a@example.org",
                  "APP_ORIGIN": "https://example.org", "APP_ENV": "production"}
        runtime = {"data": {k: deploy.base64.b64encode(v.encode()).decode() for k,v in values.items()}}
        with patch.object(deploy, "get", side_effect=[runtime, {"status": {}}]), patch.object(deploy, "helm_upgrade") as helm:
            with self.assertRaises(deploy.SetupError):
                deploy.deploy(self.args())
        helm.assert_not_called()


if __name__ == "__main__":
    unittest.main()


class RefreshArgumentTests(unittest.TestCase):
    """定期実行が渡す引数だけで通ること。

    タイマーは /opt/ へ写したスクリプトを動かす。そこには環境ファイルが無いので
    既定値は空になる。段階に関係なく全部必須にしていたため、20分ごとに
    「--agent-node が空です」で失敗し続けていた。

    失敗に気づけるのは、イメージを取り直す必要が出た時だけ。ノードに残っている
    間は動いて見えるので、いちばん遅れて表に出る。
    """

    def check(self, script, stage, argv):
        spec = importlib.util.spec_from_file_location("target_" + stage, ROOT / script)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        # 環境ファイルを読めない場所に置かれた状況にする。
        with patch.object(module.common, "environments", None), \
             patch.object(module.common, "environment_defaults", lambda *a: ("prod", {})):
            with patch.object(sys, "argv", ["x", stage, *argv]):
                with patch.object(module, stage.replace("-", "_"), lambda args: None):
                    module.main()

    def test_preview_refresh_needs_only_what_the_timer_passes(self):
        self.check("deploy-preview-gce.py", "refresh",
                   ["--project", "example-project-prod", "--image-root", "region-docker.pkg.dev/example-project-prod/repo"])

    def test_app_refresh_pull_needs_only_what_the_timer_passes(self):
        self.check("deploy-app-gce.py", "refresh-pull",
                   ["--project", "example-project-prod", "--registry-host", "region-docker.pkg.dev"])

    def test_deploy_still_demands_the_full_set(self):
        with self.assertRaises(SystemExit):
            self.check("deploy-preview-gce.py", "deploy",
                       ["--project", "example-project-prod", "--image-root", "region-docker.pkg.dev/example-project-prod/repo"])


class TenantSecretKeyTests(unittest.TestCase):
    """APIキー暗号化鍵はSecret Managerを正とし、クラスタを作り直しても変わらない。"""
    KEY = "k" * 64

    def args(self):
        return SimpleNamespace(project="example-project-prod", release="koyorina",
                               tenant_key_secret="koyorina-tenant-secret-key")

    def cluster(self, key):
        return {"data": {"TENANT_SECRET_KEY": deploy.base64.b64encode(key.encode()).decode()}} if key else None

    def ensure(self, stored, in_cluster):
        calls = []

        def run(command, payload=None):
            calls.append((command, payload))
            if command[:3] == ["gcloud", "secrets", "versions"] and command[3] == "list":
                return "projects/p/secrets/s/versions/1\n" if stored else ""
            if command[3] == "access":
                return stored
            return ""
        with patch.object(deploy, "run", side_effect=run), \
             patch.object(deploy, "get", return_value=self.cluster(in_cluster)), \
             patch.object(deploy, "upsert_secret") as upsert, patch("builtins.print"):
            deploy.ensure_tenant_secret_key(self.args())
        added = [payload for command, payload in calls if command[3] == "add"]
        return added, upsert

    def test_a_rebuilt_cluster_gets_the_key_back_from_secret_manager(self):
        added, upsert = self.ensure(self.KEY, "")
        self.assertEqual(added, [])
        upsert.assert_called_once_with("koyorina-tenant-secrets", "koyorina", {"TENANT_SECRET_KEY": self.KEY})

    def test_an_existing_cluster_key_is_uploaded_not_replaced(self):
        added, upsert = self.ensure("", self.KEY)
        self.assertEqual(added, [self.KEY])
        upsert.assert_not_called()

    def test_a_fresh_key_goes_to_secret_manager_via_stdin_before_the_cluster(self):
        added, upsert = self.ensure("", "")
        self.assertEqual(len(added), 1)
        self.assertGreaterEqual(len(added[0]), 32)
        upsert.assert_called_once_with("koyorina-tenant-secrets", "koyorina", {"TENANT_SECRET_KEY": added[0]})

    def test_matching_keys_are_left_alone(self):
        added, upsert = self.ensure(self.KEY, self.KEY)
        self.assertEqual(added, [])
        upsert.assert_not_called()

    def test_diverging_keys_stop_without_overwriting_either(self):
        with self.assertRaises(deploy.SetupError) as caught:
            self.ensure(self.KEY, "c" * 64)
        self.assertNotIn(self.KEY, str(caught.exception))
