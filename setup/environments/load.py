"""環境ごとに変わる値の唯一の置き場を読む。

ドメイン・イメージの置き場・GCPプロジェクトは、マニフェスト・Makefile・配備スクリプト・
テストの4か所から要る。同じ値をそれぞれに書くと、片方だけ直した状態で本番へ出る。
ここを通すことで、直す場所が1つに決まる。

標準ライブラリだけで動かす。配備先のVMで、依存を入れずに実行できることが要る。

    python setup/environments/load.py               # dev の値を KEY=value で並べる
    python setup/environments/load.py prod
    python setup/environments/load.py prod --value DOMAIN
    python setup/environments/load.py dev --format shell   # sh の eval 用
"""
import argparse
import json
import re
import shlex
from pathlib import Path
from string import Template

DIRECTORY = Path(__file__).resolve().parent
DEFAULT = "dev"
LINE = re.compile(r"^(?P<key>[A-Z][A-Z0-9_]*)=(?P<value>.*)$")


def names() -> list[str]:
    return sorted(path.stem for path in DIRECTORY.glob("*.env"))


def values(name: str = DEFAULT) -> dict[str, str]:
    """1つの環境の値。書き方を間違えたら黙って無視せず、その場で止める。"""
    if name not in names():
        raise SystemExit(f"環境 {name} がありません。あるのは: {', '.join(names())}")
    result: dict[str, str] = {}
    for number, raw in enumerate((DIRECTORY / f"{name}.env").read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = LINE.match(line)
        if not match:
            raise SystemExit(f"{name}.env:{number}: KEY=value の形で書いてください: {raw}")
        result[match["key"]] = match["value"].strip()
    # 置き場は環境で持ち方が違う。GCPはArtifact Registry（IMAGE_ROOT）、
    # 手元のk3sは自前のレジストリ（REGISTRY）。使う側で毎回分岐させない。
    root = result.get("IMAGE_ROOT") or result.get("REGISTRY", "")
    # Build/push callers need one resolved key. Production can keep its externally
    # reachable Artifact Registry separate from the hostname rendered into manifests.
    result["IMAGE_ROOT"] = root
    app_name = result.get("APP_NAME", "")
    result.setdefault("AGENT_IMAGE", f"{root}/{app_name}-agent:latest" if root and app_name else "")
    # マニフェストの nodeSelector に直接埋める、既に解決済みの値。
    # NODE_SELECTOR（JSON文字列）があればそれを最優先、無ければ AGENT_NODE を
    # ホスト名指定へ、どちらも無ければ {}（どのノードでもよい）にする。
    # 2か所（registry.yaml/preview.yaml）に同じ
    # if/elif を書き写さないための、ここ1か所だけの計算。
    node_selector = result.get("NODE_SELECTOR", "").strip()
    agent_node = result.get("AGENT_NODE", "").strip()
    if node_selector:
        effective = node_selector
    elif agent_node:
        effective = json.dumps({"kubernetes.io/hostname": agent_node})
    else:
        effective = "{}"
    result["EFFECTIVE_NODE_SELECTOR"] = effective
    # 公開サービスで開けておくと危ない口。書いていない環境では閉じる（既存の環境ファイルを
    # 書き足さなくても配備が止まらないように、ここで既定を補う）。
    for key in ("PREVIEW_SHELL_ENABLED", "LOCAL_CODEX_ENABLED"):
        result[key] = result.get(key) or "false"
    return result


def required(name: str, key: str) -> str:
    """空のまま使わせない。空で配備すると、名前の無いホストや置き場を指しに行く。"""
    value = values(name).get(key, "")
    if not value:
        raise SystemExit(f"{name}.env の {key} が空です。値を入れてから実行してください。")
    return value


def render(text: str, name: str = DEFAULT) -> str:
    """${KEY} を環境の値へ置き換える。知らない鍵が残っていたら止める。"""
    filled = Template(text).safe_substitute(values(name))
    unresolved = sorted(set(re.findall(r"\$\{([A-Z][A-Z0-9_]*)\}", filled)))
    if unresolved:
        raise SystemExit(f"{name}.env に無い値が使われています: {', '.join(unresolved)}")
    return filled


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("environment", nargs="?", default=DEFAULT, choices=names())
    parser.add_argument("--value", help="この鍵の値だけを出す。")
    parser.add_argument("--format", choices=["env", "shell"], default="env")
    args = parser.parse_args()
    if args.value:
        print(required(args.environment, args.value))
        return
    for key, value in values(args.environment).items():
        print(f"{key}={value}" if args.format == "env" else f"export {key}={shlex.quote(value)}")


if __name__ == "__main__":
    main()
