"""マニフェストが指すKoyorina本体のdigestを差し替える。

イメージはdigestで固定する。tagだと、同じ名前のまま中身が入れ替わり、いま何が
動いているのか後から分からなくなるため。ただしその結果、pushしたあとに
マニフェストを直さないと、apply.sh は**前と同じ古いイメージを当て直す**。
画面は何も変わらず、何も失敗しないので、原因に気づきにくい。

手で4ファイル書き換える運用をやめ、ここを通す。差し替えた結果は git diff に出るので、
「検証済みのdigestだけを当てる」という性質は変わらない。

    make pin                      # push済みのTAGのdigestを調べて差し替える
    python setup/manifest/pin.py sha256:...   # digestを直接指定する
    python setup/manifest/pin.py --check      # 全マニフェストで揃っているかだけ見る
"""
import argparse
import re
from pathlib import Path

DIRECTORY = Path(__file__).resolve().parent
# 置き場は環境で変わるのでテンプレートのまま扱う。変えるのはdigestだけ。
# アプリ名も ${APP_NAME}（setup/environments/<環境>.env）のテンプレートのまま
# マニフェストへ書いてあるので、ここで指すのもそのテンプレート文字列。
# 本体・生成を実行するagent・生成アプリを動かすpreview-runtimeは
# 別のイメージで、別々に更新する。
# preview-runtime は以前 Secret に手で入れており、ここを通っていなかった。
# そのため押し込んでも古いまま動き続け、直したはずの不具合が残った。
REPOSITORIES = ("app", "agent", "preview-runtime")
# CLIでは短い名前を使い、マニフェスト中の実際の文字列はここで対応させる。
TEMPLATE = {"app": "${APP_NAME}", "agent": "${APP_NAME}-agent",
           "preview-runtime": "${APP_NAME}-preview-runtime"}
DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


def pattern(repository: str) -> re.Pattern:
    return re.compile(r"(?P<prefix>\$\{REGISTRY\}/" + re.escape(TEMPLATE[repository])
                      + r"@)(?P<digest>sha256:[0-9a-f]{64})(?![0-9a-f])")


def pins(repository: str) -> dict[Path, set[str]]:
    found = {}
    for path in sorted(DIRECTORY.glob("*.yaml")):
        digests = {match["digest"]
                   for match in pattern(repository).finditer(path.read_text(encoding="utf-8"))}
        if digests:
            found[path] = digests
    return found


def apply(repository: str, digest: str) -> list[str]:
    changed = []
    for path in pins(repository):
        text = path.read_text(encoding="utf-8")
        updated = pattern(repository).sub(lambda m: m["prefix"] + digest, text)
        if updated != text:
            path.write_text(updated, encoding="utf-8")
            changed.append(path.name)
    return changed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("digest", nargs="?", help="sha256:...")
    parser.add_argument("--repository", default="app", choices=REPOSITORIES)
    parser.add_argument("--check", action="store_true", help="差し替えず、揃っているかだけ見る。")
    args = parser.parse_args()
    if args.check:
        for repository in REPOSITORIES:
            print(f"{repository}: {' / '.join(sorted(every(repository)))}")
        return
    current = every(args.repository)
    if not args.digest:
        raise SystemExit("差し替えるdigestを指定してください（make pin が調べて渡します）。")
    if not DIGEST.fullmatch(args.digest):
        raise SystemExit("digestは sha256: に続く64桁の16進で指定してください。")
    if current == {args.digest}:
        print(f"{args.repository}: すでに {args.digest} を指しています。")
        return
    changed = apply(args.repository, args.digest)
    print(f"{args.repository}: {' / '.join(sorted(current))} → {args.digest}")
    print("差し替え: " + "、".join(changed))
    print("git diff で確認してから setup/manifest/apply.sh を実行してください。")


def every(repository: str) -> set[str]:
    current = pins(repository)
    if not current:
        raise SystemExit(f"{repository} をdigestで固定しているマニフェストが見つかりません。")
    digests = {digest for values in current.values() for digest in values}
    if len(digests) != 1:
        raise SystemExit(f"{repository} のdigestがマニフェストごとに違います。揃えてください: "
                         + ", ".join(f"{path.name}={sorted(values)}"
                                     for path, values in current.items()))
    return digests


if __name__ == "__main__":
    main()
