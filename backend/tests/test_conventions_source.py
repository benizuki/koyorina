"""共通部品の正が1つであること、そのままイメージ外へ差し替えられること。"""
import pytest
from backend.domain.generation import (PACKAGED_CONVENTIONS, conventions, conventions_root,
                                       scaffold_files)
from setup.conventions.sync import MIRRORED, SCAFFOLD, differences


def test_skills_templates_match_the_single_source():
    """Skillsの雛形は正の写し。ずれたら同期スクリプトで揃える。"""
    assert differences() == []


def test_every_mirrored_file_exists_in_the_source():
    for source in MIRRORED:
        assert (SCAFFOLD / source).is_file(), source


def test_default_root_is_the_packaged_one(monkeypatch):
    monkeypatch.delenv("CONVENTIONS_ROOT", raising=False)
    assert conventions_root() == PACKAGED_CONVENTIONS
    assert "AGENTS" in conventions() or conventions().strip()
    assert "frontend/src/styles/tokens.css" in scaffold_files()


def test_root_can_be_replaced_without_rebuilding_the_image(tmp_path, monkeypatch):
    """イメージを作り直さずに規約を差し替えられる。ここが目的。"""
    (tmp_path / "AGENTS.md").write_text("置き換えた規約\n", encoding="utf-8")
    scaffold = tmp_path / "scaffold" / "frontend" / "src" / "styles"
    scaffold.mkdir(parents=True)
    (scaffold / "tokens.css").write_text(":root { --x: 1px; }\n", encoding="utf-8")
    monkeypatch.setenv("CONVENTIONS_ROOT", str(tmp_path))
    # 差し替えるのは規約の本文。スキルの一覧は実際に配られたものから足すので、
    # 本文だけを比べる。
    assert conventions(with_skills=False) == "置き換えた規約\n"
    assert conventions().startswith("置き換えた規約\n")
    assert scaffold_files() == {"frontend/src/styles/tokens.css": ":root { --x: 1px; }\n"}


@pytest.mark.parametrize("layout", ["empty", "no_scaffold", "no_agents"])
def test_a_misconfigured_root_fails_loudly(tmp_path, monkeypatch, layout):
    """黙って同梱版へ戻さない。規約なしで生成が進むほうが危ない。"""
    if layout != "empty":
        if layout == "no_scaffold":
            (tmp_path / "AGENTS.md").write_text("x", encoding="utf-8")
        else:
            (tmp_path / "scaffold").mkdir()
    monkeypatch.setenv("CONVENTIONS_ROOT", str(tmp_path))
    with pytest.raises(RuntimeError, match="CONVENTIONS_ROOT"):
        conventions()


def test_blank_root_falls_back_to_the_packaged_one(monkeypatch):
    """未設定と空文字を同じ扱いにする。空のenvを配っても壊れないように。"""
    monkeypatch.setenv("CONVENTIONS_ROOT", "   ")
    assert conventions_root() == PACKAGED_CONVENTIONS


def test_font_token_only_names_fonts_that_are_loaded():
    """読み込んでいないフォントをトークンの先頭に置かない。
    入っている端末だけ別の描画になり、生成アプリ側で見た目がずれる。"""
    files = scaffold_files()
    tokens = files["frontend/src/styles/tokens.css"]
    loaded = files["frontend/src/styles/base.css"]
    mono = next(line for line in tokens.splitlines() if "--font-mono:" in line)
    for family in ("IBM Plex Mono",):
        assert family not in mono or family.replace(" ", "+") in loaded


def controller_settings(**overrides):
    from backend.worker.controller import ControllerSettings
    base = {"_env_file": None, "token": "controller-token-32-characters-long!!",
            "agent_image": "registry.example.com/koyorina-agent@sha256:" + "a" * 64}
    return ControllerSettings(**{**base, **overrides})


def agent_pod(**overrides):
    from uuid import UUID
    from backend.worker.controller import resources
    return resources(UUID("a5811e7e-a100-41d3-9100-0412e340ca68"),
                     controller_settings(**overrides))["pods"]["spec"]


def test_agent_uses_the_packaged_conventions_by_default():
    container = agent_pod()["containers"][0]
    assert not [e for e in container["env"] if e["name"] == "CONVENTIONS_ROOT"]
    assert not [m for m in container["volumeMounts"] if m["name"] == "conventions"]


def test_agent_reads_conventions_from_a_volume_when_one_is_configured():
    """規約の差し替えにagentイメージの作り直しを要らなくする。"""
    spec = agent_pod(conventions_claim="koyorina-conventions")
    container = spec["containers"][0]
    root = next(e for e in container["env"] if e["name"] == "CONVENTIONS_ROOT")
    mount = next(m for m in container["volumeMounts"] if m["name"] == "conventions")
    volume = next(v for v in spec["volumes"] if v["name"] == "conventions")
    assert root["value"] == mount["mountPath"] == "/run/conventions"
    assert mount["readOnly"] is True
    assert volume["persistentVolumeClaim"] == {"claimName": "koyorina-conventions", "readOnly": True}


def css_tokens(path):
    """`:root` の直下に書かれたトークンを読む。配色の切り替え分は含めない。"""
    import re
    text = path.read_text(encoding="utf-8")
    start = text.index("{", text.index(":root"))
    depth = 0
    for index in range(start, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                break
    body = text[start:index]
    return {name: value.strip()
            for name, value in re.findall(r"^\s*(--[a-z0-9-]+)\s*:\s*([^;]+);", body, re.M)}


def test_generated_apps_use_the_same_palette_as_app_forge():
    """生成アプリの配色を、Koyorina 本体と同じにする。

    雛形と本体で別々に育ってしまい、31トークンがずれていた。並べて置くと
    別のアプリに見えるし、本体の配色を直しても生成側へ伝わらない。
    """
    from backend.domain.generation import PACKAGED_CONVENTIONS

    root = PACKAGED_CONVENTIONS.parent.parent
    app = css_tokens(root / "frontend/src/styles/tokens.css")
    scaffold = css_tokens(PACKAGED_CONVENTIONS / "scaffold/frontend/src/styles/tokens.css")
    assert scaffold, "雛形のトークンを読めていない"

    differing = {name: (scaffold[name], app[name]) for name in scaffold
                 if name in app and scaffold[name] != app[name]}
    assert not differing, f"本体と値が違うトークン: {sorted(differing)}"
    # 本体が知らないトークンを雛形が持つと、本体を直しても追随できない。
    assert not [name for name in scaffold if name not in app], "本体に無いトークンが雛形にある"


def palette_tokens(path, palette):
    """その配色で実際に効く値。既定（緑）に、配色ごとの上書きを重ねる。"""
    import re
    text = path.read_text(encoding="utf-8")
    selector = ":root {" if palette == "green" else f':root[data-palette="{palette}"]'
    values = css_tokens(path) if palette == "green" else dict(css_tokens(path))
    if palette != "green":
        start = text.index("{", text.index(selector))
        depth = 0
        for index in range(start, len(text)):
            if text[index] == "{":
                depth += 1
            elif text[index] == "}":
                depth -= 1
                if depth == 0:
                    break
        values.update(dict(re.findall(r"^\s*(--[a-z0-9-]+)\s*:\s*([^;]+);", text[start:index], re.M)))
    return {name: value.strip() for name, value in values.items()}


def test_the_vuetify_theme_matches_those_tokens_for_every_palette():
    """Vuetify には hex を直接渡すため、トークンと同じ値が2か所に出る。

    片方だけ変えると、`v-btn` の色と手書きCSSの色が少しずつ違う画面ができる。
    配色ごとの上書きも、その配色で効く値と突き合わせる（既定の値と比べると、
    青系の primary を緑の accent-solid と比べることになって意味がない）。
    """
    import re
    from backend.domain.generation import PACKAGED_CONVENTIONS

    tokens_path = PACKAGED_CONVENTIONS / "scaffold/frontend/src/styles/tokens.css"
    defaults = (PACKAGED_CONVENTIONS / "scaffold/frontend/src/styles/vuetify-defaults.ts").read_text()
    names = {"themeColors": "green", "bluePalette": "blue",
             "redPalette": "red", "yellowPalette": "yellow"}
    checked = 0
    for symbol, palette in names.items():
        assert f"const {symbol} = {{" in defaults, f"{symbol} が雛形に無い"
        body = defaults[defaults.index(f"const {symbol} = {{"):]
        body = body[:body.index("} as const")]
        wanted = palette_tokens(tokens_path, palette)
        for index, mode in enumerate(("light", "dark")):
            section = body[body.index(f"{mode}: {{"):]
            section = section[:section.index("}")]
            for hex_value, token in re.findall(r"'(#[0-9a-f]{6})',\s*// = (--[a-z0-9-]+)", section):
                declared = wanted[token]
                pair = re.match(r"light-dark\((#[0-9a-f]{6}),\s*(#[0-9a-f]{6})\)", declared)
                assert pair, f"{token} の書式が light-dark ではない: {declared}"
                assert pair.group(index + 1) == hex_value, (
                    f"{palette}/{mode} {token} が {pair.group(index + 1)}、Vuetify は {hex_value}")
                checked += 1
    assert checked >= 20, f"突き合わせた数が少なすぎる: {checked}"


def test_generated_apps_can_switch_palette_like_app_forge():
    """配色の切り替えを、生成アプリにも同じだけ持たせる。

    雛形はライト/ダークだけで、配色の軸が無かった。並べて置くと、本体では
    選べる配色が生成アプリでは選べない、という差になる。
    """
    from backend.domain.generation import PACKAGED_CONVENTIONS, scaffold_files

    files = scaffold_files()
    tokens = files["frontend/src/styles/tokens.css"]
    for palette in ("blue", "red", "yellow"):
        assert f'data-palette="{palette}"' in tokens, palette
        assert f"--swatch-{palette}" in tokens, palette
    # 起動前に配色を戻さないと、緑で一瞬描かれてから切り替わる。
    assert "theme-palette" in files["frontend/public/theme-init.js"]
    assert "yellow" in files["frontend/public/theme-init.js"]
    # 選択盤は2行2列。増えるたびに横へ広がらないように。
    toggle = files["frontend/src/components/ThemeToggle.vue"]
    assert "grid-template-columns: 1fr 1fr" in toggle
    assert "PALETTES" in toggle
