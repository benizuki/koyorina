"""マニフェストを環境の値で埋めて出す。適用するのは apply.sh。

マニフェストは `${DOMAIN}` のようなテンプレートで持つ。環境ごとに写しを作ると、
片方だけ直した状態が必ず生まれ、どちらが正か分からなくなる。

テンプレートのまま kubectl へ渡さないこと。`${DOMAIN}` のままのホスト名は
どのリクエストにも一致せず、起動はするのに誰も繋がらない状態になる。埋め残しが
あれば load.render が止めるので、その状態は出口まで届かない。

    python setup/manifest/render.py app.yaml                 # dev の値で標準出力へ
    python setup/manifest/render.py --all --out /tmp/forge   # 全部をディレクトリへ
    python setup/manifest/render.py --all --out /tmp/forge --environment prod
"""
import argparse
import importlib.util
import os
from pathlib import Path

DIRECTORY = Path(__file__).resolve().parent
SETUP = DIRECTORY.parent
_spec = importlib.util.spec_from_file_location(
    "forge_environments", SETUP / "environments" / "load.py")
environments = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(environments)


def manifests() -> list[Path]:
    return sorted(DIRECTORY.glob("*.yaml"))


def resolve(name: str) -> Path:
    """setup/manifest の名前でも、setup/ からのパスでも受ける。

    ingress.yaml のように manifest/ の外にあるものも、同じ仕組みで埋める。
    手で書き換える場所を1つでも残すと、そこだけ古い環境を指し続ける。
    """
    for candidate in (DIRECTORY / name, SETUP / name):
        resolved = candidate.resolve()
        if resolved.is_file() and SETUP in resolved.parents:
            return resolved
    raise SystemExit(f"{name} は setup/ にありません。")


def rendered(path: Path, environment: str) -> str:
    return environments.render(path.read_text(encoding="utf-8"), environment)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("name", nargs="?", help="マニフェスト名（例: app.yaml）")
    parser.add_argument("--all", action="store_true", help="全マニフェストを書き出す。")
    parser.add_argument("--out", type=Path, help="書き出し先のディレクトリ。")
    # 既定は環境変数 ENV。apply.sh も make も同じ呼び方で揃う。
    parser.add_argument("--environment", default=os.environ.get("ENV", environments.DEFAULT),
                        choices=environments.names())
    args = parser.parse_args()
    if args.all == bool(args.name):
        raise SystemExit("マニフェスト名か --all のどちらかを指定してください。")
    targets = manifests() if args.all else [resolve(args.name)]
    if not args.out:
        if args.all:
            raise SystemExit("--all には --out が要ります。")
        print(rendered(targets[0], args.environment), end="")
        return
    args.out.mkdir(parents=True, exist_ok=True)
    for path in targets:
        (args.out / path.name).write_text(rendered(path, args.environment), encoding="utf-8")
    print(f"{len(targets)} 件を {args.out} へ書き出しました（{args.environment}）。")


if __name__ == "__main__":
    main()
