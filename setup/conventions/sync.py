#!/usr/bin/env python3
"""共通部品の正は backend/conventions/scaffold/。Skillsの雛形へその内容を配る。

Skillsの assets はプロジェクト雛形一式で、そのうち数ファイルが共通部品と重なる。
重なる分をコピーで持つと、必ず片方だけ直されてずれる。ここを唯一の配り口にする。

  python -m setup.conventions.sync --check   ずれていたら失敗する（テストが使う）
  python -m setup.conventions.sync           正の内容でSkills側を上書きする
"""
import argparse
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parent.parent.parent
SCAFFOLD = ROOT / "backend" / "conventions" / "scaffold"

# 正のパス（scaffold内） -> 配布先（リポジトリ内）
MIRRORED = {
    "backend/core/security_headers.py":
        "Skills/fastapi-backend/assets/core/security_headers.py",
    "frontend/public/theme-init.js":
        "Skills/vue-vuetify-frontend/assets/public/theme-init.js",
    "frontend/src/components/ThemeToggle.vue":
        "Skills/vue-vuetify-frontend/assets/src/components/ThemeToggle.vue",
    "frontend/src/composables/useThemeMode.ts":
        "Skills/vue-vuetify-frontend/assets/src/composables/useThemeMode.ts",
    "frontend/src/styles/base.css":
        "Skills/vue-vuetify-frontend/assets/src/styles/base.css",
    "frontend/src/styles/tokens.css":
        "Skills/vue-vuetify-frontend/assets/src/styles/tokens.css",
    "frontend/src/styles/vuetify-defaults.ts":
        "Skills/vue-vuetify-frontend/assets/src/styles/vuetify-defaults.ts",
}


# 配色の正は本体のフロントエンド。雛形はその写し。
# 本体の tokens.css を直しただけで生成アプリへ伝わらないと、並べたときに
# 別のアプリに見える（実際、31トークンがずれていた）。
APP_TOKENS = ROOT / "frontend" / "src" / "styles" / "tokens.css"
SCAFFOLD_TOKENS = SCAFFOLD / "frontend" / "src" / "styles" / "tokens.css"
PALETTE_HEADING = "/* ------------------------------------------------------------------\n * 配色（palette）"
TOKEN_LINE = re.compile(r"^(\s*)(--[a-z0-9-]+)(\s*:\s*)([^;]+)(;)", re.M)


def root_span(text: str) -> tuple[int, int]:
    start = text.index("{", text.index(":root"))
    depth = 0
    for index in range(start, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return start, index
    raise SystemExit("tokens.css の :root を閉じられません")


def coloured_scaffold() -> str:
    """本体の値を雛形へ写したもの。雛形にしかない行は触らない。"""
    app = APP_TOKENS.read_text(encoding="utf-8")
    a, b = root_span(app)
    values = {name: value.strip() for _, name, _, value, _ in TOKEN_LINE.findall(app[a:b])}

    text = SCAFFOLD_TOKENS.read_text(encoding="utf-8")
    i, j = root_span(text)

    def replace(match):
        indent, name, separator, value, end = match.groups()
        wanted = values.get(name)
        return match[0] if wanted is None else f"{indent}{name}{separator}{wanted}{end}"

    head = text[:i] + TOKEN_LINE.sub(replace, text[i:j])
    tail = text[j:]
    # 配色の切り替えブロックは、まるごと本体のものに置き換える。
    if PALETTE_HEADING in tail:
        tail = tail[:tail.index(PALETTE_HEADING)]
    return head + tail.rstrip("\n") + "\n\n" + app[app.index(PALETTE_HEADING):].rstrip("\n") + "\n"


APP_MAIN = ROOT / "frontend" / "src" / "main.ts"
SCAFFOLD_DEFAULTS = SCAFFOLD / "frontend" / "src" / "styles" / "vuetify-defaults.ts"
# Vuetify には hex を直接渡すため、トークンと同じ値が main.ts にも出る。
# 本体で配色を直したら、こちらも一緒に運ぶ。片方だけだと v-btn の色がずれる。
PALETTE_CONSTANTS = ("themeColors", "bluePalette", "redPalette", "yellowPalette")


def constant(text: str, name: str) -> str:
    start = text.index(f"const {name} = {{")
    return text[start:text.index("} as const", start) + len("} as const")]


def coloured_defaults() -> str:
    app = APP_MAIN.read_text(encoding="utf-8")
    text = SCAFFOLD_DEFAULTS.read_text(encoding="utf-8")
    for name in PALETTE_CONSTANTS:
        text = text.replace(constant(text, name), constant(app, name), 1)
    return text


def differences() -> list[str]:
    """ずれているものを、人が読める1行にして返す。"""
    out = []
    if SCAFFOLD_TOKENS.read_text(encoding="utf-8") != coloured_scaffold():
        out.append("配色が本体とずれています: backend/conventions/scaffold/"
                   "frontend/src/styles/tokens.css")
    if SCAFFOLD_DEFAULTS.read_text(encoding="utf-8") != coloured_defaults():
        out.append("Vuetifyのテーマ色が本体とずれています: backend/conventions/scaffold/"
                   "frontend/src/styles/vuetify-defaults.ts")
    for source, target in sorted(MIRRORED.items()):
        origin, copy = SCAFFOLD / source, ROOT / target
        if not origin.is_file():
            out.append(f"正が見つかりません: backend/conventions/scaffold/{source}")
        elif not copy.is_file():
            out.append(f"配布先がありません: {target}")
        elif origin.read_bytes() != copy.read_bytes():
            out.append(f"内容がずれています: {target}")
    return out


def apply() -> list[str]:
    changed = []
    # 先に本体 → 雛形。そのあと雛形 → Skills の順でないと、1回では揃わない。
    coloured = coloured_scaffold()
    if SCAFFOLD_TOKENS.read_text(encoding="utf-8") != coloured:
        SCAFFOLD_TOKENS.write_text(coloured, encoding="utf-8")
        changed.append("backend/conventions/scaffold/frontend/src/styles/tokens.css")
    defaults = coloured_defaults()
    if SCAFFOLD_DEFAULTS.read_text(encoding="utf-8") != defaults:
        SCAFFOLD_DEFAULTS.write_text(defaults, encoding="utf-8")
        changed.append("backend/conventions/scaffold/frontend/src/styles/vuetify-defaults.ts")
    for source, target in sorted(MIRRORED.items()):
        origin, copy = SCAFFOLD / source, ROOT / target
        data = origin.read_bytes()
        if not copy.is_file() or copy.read_bytes() != data:
            copy.parent.mkdir(parents=True, exist_ok=True)
            copy.write_bytes(data)
            changed.append(target)
    return changed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="上書きせず、ずれだけを報告する")
    if parser.parse_args().check:
        if problems := differences():
            print("共通部品がずれています。"
                  "`python3 -m setup.conventions.sync` で揃えてください。")
            print("\n".join("  - " + line for line in problems))
            return 1
        print(f"共通部品は揃っています（{len(MIRRORED)}ファイル）。")
        return 0
    if changed := apply():
        print("\n".join("更新: " + name for name in changed))
    else:
        print("変更はありません。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
