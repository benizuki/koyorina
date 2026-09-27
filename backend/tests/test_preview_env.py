"""プレビューの環境変数。指定できるものと、指定させないもの。"""
import pytest
from uuid import UUID
from backend.domain import preview_env
from backend.domain.preview import runtime_environment


def entry(name, value=None, secret=False):
    return {"name": name, "value": value, "secret": secret}


# ---- 予約名 -----------------------------------------------------------------

def test_reserved_names_come_from_the_runtime_itself():
    """向こうにキーが増えても、こちらの守りが遅れないようにする。"""
    from backend.domain.preview import PACKAGE_SOURCE, package_source
    assert preview_env.RESERVED == (set(runtime_environment(UUID(int=0), "", "", "", "", ""))
                                    | set(PACKAGE_SOURCE))
    # 取得元は未設定でも予約する。値が空なら渡さないので、名前だけでは守れない。
    assert not package_source()
    assert set(package_source("https://npm.example", "7", "https://pypi.example/simple/")) \
        <= set(PACKAGE_SOURCE)


def test_the_package_source_cannot_be_redirected_from_the_screen():
    """取得元を画面から差し替えられると、検査済みの経路を外れてしまう。"""
    for name in ("NPM_CONFIG_REGISTRY", "UV_DEFAULT_INDEX", "NPM_CONFIG_MIN_RELEASE_AGE"):
        with pytest.raises(preview_env.InvalidEnvironment, match="Koyorinaが設定する"):
            preview_env.parse([entry(name, "https://elsewhere.example")])


@pytest.mark.parametrize("name", ["DATABASE_URL", "APP_FORWARD_SECRET", "APP_SESSION_SECRET",
                                  "APP_BASE_PATH", "FRONTEND_DIST", "APP_ORIGIN"])
def test_platform_keys_cannot_be_set(name):
    with pytest.raises(preview_env.InvalidEnvironment, match="Koyorinaが設定する"):
        preview_env.parse([entry(name, "x")])


def test_the_platform_wins_even_if_a_reserved_name_slips_through():
    """検査を通らない経路が増えても壊れないよう、順序でも守る。"""
    result = runtime_environment(UUID(int=1), "o", "s", "f", "g", "a",
                                 extra={"DATABASE_URL": "postgres://attacker/", "API_KEY": "k"})
    assert result["DATABASE_URL"].startswith("sqlite+pysqlite:")
    assert result["API_KEY"] == "k"


def test_a_reserved_name_is_dropped_on_the_way_out_too():
    stored = [entry("DATABASE_URL", "postgres://attacker/"), entry("API_KEY", "k")]
    assert preview_env.as_environment(stored) == {"API_KEY": "k"}


# ---- 名前と値の検査 ---------------------------------------------------------

@pytest.mark.parametrize("name", ["lower", "1LEADING", "WITH-DASH", "WITH SPACE", "", "A" * 65])
def test_a_malformed_name_is_refused(name):
    with pytest.raises(preview_env.InvalidEnvironment):
        preview_env.parse([entry(name, "x")])


@pytest.mark.parametrize("value", ["line\nbreak", "null\x00byte", "bell\x07"])
def test_control_characters_are_refused(value):
    with pytest.raises(preview_env.InvalidEnvironment, match="使えない文字"):
        preview_env.parse([entry("API_KEY", value)])


def test_an_oversized_value_is_refused():
    with pytest.raises(preview_env.InvalidEnvironment, match="長すぎ"):
        preview_env.parse([entry("API_KEY", "x" * (preview_env.MAX_VALUE_BYTES + 1))])


def test_too_many_entries_are_refused():
    many = [entry(f"K{i}", "v") for i in range(preview_env.MAX_ENTRIES + 1)]
    with pytest.raises(preview_env.InvalidEnvironment, match="件まで"):
        preview_env.parse(many)


def test_duplicate_names_are_refused():
    with pytest.raises(preview_env.InvalidEnvironment, match="重複"):
        preview_env.parse([entry("API_KEY", "a"), entry("API_KEY", "b")])


def test_a_plain_entry_must_carry_a_value():
    with pytest.raises(preview_env.InvalidEnvironment, match="値を入力"):
        preview_env.parse([entry("API_BASE")])


# ---- 表示（A=そのまま / B=伏せる） ------------------------------------------

def test_a_plain_entry_is_shown_as_it_is():
    shown = preview_env.visible([entry("API_BASE", "https://example.test")])
    assert shown == [{"name": "API_BASE", "secret": False,
                      "value": "https://example.test", "configured": True}]


def test_a_secret_value_never_leaves_the_server():
    shown = preview_env.visible([entry("API_KEY", "super-secret", secret=True)])
    assert shown == [{"name": "API_KEY", "secret": True, "value": None, "configured": True}]
    assert "super-secret" not in repr(shown)


def test_a_secret_still_reaches_the_running_application():
    """画面に出さないだけで、アプリへは渡す。渡らないと意味がない。"""
    assert preview_env.as_environment([entry("API_KEY", "s", secret=True)]) == {"API_KEY": "s"}


# ---- 編集（秘密を消さない） -------------------------------------------------

def test_editing_another_entry_keeps_an_untouched_secret():
    stored = [entry("API_BASE", "https://old"), entry("API_KEY", "kept", secret=True)]
    incoming = preview_env.parse([entry("API_BASE", "https://new"), entry("API_KEY", secret=True)])
    merged = preview_env.merge(stored, incoming)
    assert {e["name"]: e["value"] for e in merged} == {"API_BASE": "https://new", "API_KEY": "kept"}


def test_a_secret_can_be_replaced_by_sending_a_new_value():
    stored = [entry("API_KEY", "old", secret=True)]
    merged = preview_env.merge(stored, preview_env.parse([entry("API_KEY", "new", secret=True)]))
    assert merged[0]["value"] == "new"


def test_an_entry_left_out_is_removed():
    stored = [entry("A", "1"), entry("B", "2")]
    merged = preview_env.merge(stored, preview_env.parse([entry("A", "1")]))
    assert preview_env.names(merged) == ["A"]


def test_a_new_secret_without_a_value_is_refused():
    """まだ保存されていないのに値が無いなら、残すものが無い。"""
    with pytest.raises(preview_env.InvalidEnvironment, match="値を入力"):
        preview_env.merge([], preview_env.parse([entry("API_KEY", secret=True)]))


def test_names_are_listed_without_values():
    stored = [entry("B", "2"), entry("A", "1", secret=True)]
    assert preview_env.names(stored) == ["A", "B"]
