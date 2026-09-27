"""開発用の認証スキップが、本番で有効にならないことを確かめる。

このテストがある意味は「便利機能の動作確認」ではなく、
**認証が外れたまま本番に出る事故を止めること**。
dev_auth.py を触ったら必ずここを通す。
"""
from __future__ import annotations

import importlib

import pytest

import core.dev_auth as dev_auth


def _reload(monkeypatch, **env):
    """環境変数を差し替えてモジュールを読み直す。

    APP_ENV はモジュール読み込み時に一度だけ評価するので、
    monkeypatch.setenv だけでは反映されない。
    """
    for key in ("APP_ENV", "DEV_USER_EMAIL", "DEV_USER_ROLE", *dev_auth._CLOUD_MARKERS):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return importlib.reload(dev_auth)


def test_disabled_by_default(monkeypatch):
    # APP_ENV 未設定 = production 扱い。設定し忘れたら安全側に倒れること。
    module = _reload(monkeypatch)
    assert module.dev_auth_enabled() is False


def test_disabled_for_any_value_other_than_local(monkeypatch):
    for value in ("production", "prod", "staging", "development", "dev", "true", ""):
        module = _reload(monkeypatch, APP_ENV=value)
        assert module.dev_auth_enabled() is False, f"APP_ENV={value!r} で有効になってはいけない"


def test_enabled_only_for_local(monkeypatch):
    module = _reload(monkeypatch, APP_ENV="local")
    assert module.dev_auth_enabled() is True
    module.assert_safe_environment()  # クラウド判定に当たらなければ通る


@pytest.mark.parametrize("marker", ["K_SERVICE", "K_REVISION", "GAE_ENV", "KUBERNETES_SERVICE_HOST"])
def test_refuses_to_start_on_cloud(monkeypatch, marker):
    # クラウドの環境変数はプラットフォームが入れるので、
    # アプリ側の設定ミスでは消えない。ここで起動を止める。
    module = _reload(monkeypatch, APP_ENV="local", **{marker: "some-value"})
    with pytest.raises(RuntimeError, match="APP_ENV=local"):
        module.assert_safe_environment()


def test_production_on_cloud_starts_normally(monkeypatch):
    module = _reload(monkeypatch, K_SERVICE="my-app")
    module.assert_safe_environment()  # 例外が出ないこと


def test_dev_user_is_configurable(monkeypatch):
    module = _reload(monkeypatch, APP_ENV="local", DEV_USER_EMAIL="tester@example.com", DEV_USER_ROLE="general")
    assert module.dev_user()["email"] == "tester@example.com"
    assert module.dev_role() == "general"
    # general は参照のみ。権限で隠す画面を確認できること。
    assert module.dev_permissions()["can_view"] is True
    assert module.dev_permissions()["can_manage_users"] is False
