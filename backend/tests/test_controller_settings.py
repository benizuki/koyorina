"""Controller destination validation; no database or cloud access."""
import os
import unittest
from unittest.mock import patch

from pydantic import ValidationError

from backend.config.settings import Settings


class ControllerSettingsTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)

    def settings(self, name="koyorina", **overrides):
        values = dict(
            app_name=name, app_env="production",
            database_url="postgresql+psycopg://unused/unused",
            app_origin="https://example.test", app_session_secret="s" * 40,
            google_oauth_client_id="test-client",
            codex_controller_url=f"http://{name}-codex-controller.{name}-codex.svc:8080",
            codex_controller_token="c" * 40,
            preview_enabled=True, preview_backend="controller",
            preview_controller_url=f"http://{name}-preview-controller.{name}-preview.svc:8080",
            preview_controller_token="p" * 40,
        )
        values.update(overrides)
        return Settings(_env_file=None, **values)

    def test_default_and_renamed_services(self):
        for name in ("koyorina", "ai-terakoya", "a", "a" * 44):
            with self.subTest(name=name):
                self.assertEqual(self.settings(name).app_name, name)

    def test_app_name_from_environment_and_default(self):
        self.assertEqual(self.settings(app_name="koyorina").app_name, "koyorina")
        values = self.settings("ai-terakoya").model_dump()
        values.pop("app_name")
        with patch.dict(os.environ, {"APP_NAME": "ai-terakoya"}):
            self.assertEqual(Settings(_env_file=None, **values).app_name, "ai-terakoya")
        values = self.settings().model_dump()
        values.pop("app_name")
        self.assertEqual(Settings(_env_file=None, **values).app_name, "koyorina")

    def test_other_destinations_are_rejected(self):
        for role in ("codex", "preview"):
            expected = f"http://ai-terakoya-{role}-controller.ai-terakoya-{role}.svc:8080"
            for url in (
                f"http://koyorina-{role}-controller.koyorina-{role}.svc:8080",
                f"http://ai-terakoya-{role}-controller.other-{role}.svc:8080",
                "http://attacker.example", "http://169.254.169.254",
                expected + ".evil.test", expected + "/path", expected + "?q=1",
                expected.replace(":8080", ":80"),
            ):
                with self.subTest(role=role, url=url), self.assertRaises(ValidationError):
                    self.settings("ai-terakoya", **{f"{role}_controller_url": url})

    def test_short_tokens_are_rejected(self):
        for role in ("codex", "preview"):
            with self.subTest(role=role), self.assertRaises(ValidationError):
                self.settings("ai-terakoya", **{f"{role}_controller_token": "short"})

    def test_invalid_app_names_are_rejected(self):
        for name in ("", "Name", "a.b", "a/evil", "-a", "a-", "a\n", "a" * 45):
            with self.subTest(name=name), self.assertRaises(ValidationError):
                self.settings(name)

    def test_local_codex_exception_stays_local(self):
        self.settings(app_env="local", app_origin="http://127.0.0.1:8080",
                      codex_controller_url="http://127.0.0.1:8091",
                      preview_controller_url="http://127.0.0.1:8092")
        with self.assertRaises(ValidationError):
            self.settings(codex_controller_url="http://127.0.0.1:8091")
