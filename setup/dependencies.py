"""依存の版を上げる。解決し直して、pyproject.toml の下限をそこへ揃える。

版の宣言は pyproject.toml だけ。解決済みの版を別ファイルへ固定することはしない。

    python3 setup/dependencies.py            # 解決し直して下限を揃える
    python3 setup/dependencies.py --check    # 追いついているかだけ見る（書き換えない）

下限は「これ未満では動かない」という宣言で、実際に入る版ではない。だから
`fastapi>=0.115` のままでも最新の 0.141 が入る。混同しやすいので、宣言と実物を
離さない（離れているほど「古いものが入っているのでは」と疑う時間が増える）。

上限（`<1` や `<3`）は動かさない。メジャーが上がるときは、何が壊れるかを
人が読んでから決める。
"""
import argparse
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = ROOT / "pyproject.toml"

# name==version だけを拾う。uv は "    # via ..." を続けて書く。
PIN = re.compile(r"^([A-Za-z0-9._-]+)==([^\s;]+)", re.MULTILINE)

# fastapi>=0.115,<1 / uvicorn[standard]>=0.34,<1 / websockets>=13,<16
FLOOR = re.compile(r'"(?P<name>[A-Za-z0-9._-]+)(?P<extras>\[[^\]]*\])?>=(?P<floor>[^,"]+)'
                   r'(?P<rest>[^"]*)"')


def resolve() -> dict:
    """いまの宣言のなかで、いちばん新しい組み合わせへ解決する。"""
    # -o は付けない。"-" を渡すと、その名前のファイルが作られる（標準出力にならない）。
    result = subprocess.run(
        ["uv", "pip", "compile", str(PYPROJECT), "--upgrade", "--no-header", "--quiet"],
        capture_output=True, text=True, cwd=ROOT)

    if result.returncode:
        raise SystemExit("依存を解決できませんでした。\n" + result.stderr.strip()[:2000])

    return {name.lower().replace("_", "-"): version for name, version in PIN.findall(result.stdout)}


def raise_floors(pyproject: str, resolved: dict) -> tuple[str, list]:
    """下限を解決結果へ揃える。上限と extras はそのまま残す。"""
    changed = []

    def replace(match):
        name = match["name"].lower().replace("_", "-")
        new = resolved.get(name)

        if not new or new == match["floor"]:
            return match[0]

        changed.append(f'{match["name"]}: {match["floor"]} → {new}')

        return f'"{match["name"]}{match["extras"] or ""}>={new}{match["rest"]}"'

    return FLOOR.sub(replace, pyproject), changed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="書き換えず、差があるかだけ見る。")
    args = parser.parse_args()

    resolved = resolve()
    pyproject = PYPROJECT.read_text(encoding="utf-8")
    updated, changed = raise_floors(pyproject, resolved)

    if args.check:
        print("下限は解決結果に揃っています。" if not changed else "下限を上げられます:")
        for line in changed:
            print("  " + line)
        raise SystemExit(0 if not changed else 1)

    if not changed:
        print("下限はすでに解決結果に揃っています。")
        return

    PYPROJECT.write_text(updated, encoding="utf-8")

    print("pyproject.toml の下限を上げました:")

    for line in changed:
        print("  " + line)

    print("git diff で確認し、テストを通してからコミットしてください。")


if __name__ == "__main__":
    sys.exit(main())
