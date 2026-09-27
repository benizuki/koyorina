#!/bin/sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
VERSION=v0.19.7
OS=$(uname -s | tr '[:upper:]' '[:lower:]')
case "$OS" in darwin|linux) ;; *) echo "対応していないOSです: $OS" >&2; exit 2;; esac
case $(uname -m) in x86_64|amd64) ARCH=amd64;; arm64|aarch64) ARCH=arm64;; *) echo "対応していないCPUアーキテクチャです" >&2; exit 2;; esac
DEST="$ROOT/.runtime/bin"
mkdir -p "$DEST"
ARCHIVE="cilium-$OS-$ARCH.tar.gz"
BASE="https://github.com/cilium/cilium-cli/releases/download/$VERSION"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
curl -fsSL "$BASE/$ARCHIVE" -o "$TMP/$ARCHIVE"
curl -fsSL "$BASE/$ARCHIVE.sha256sum" -o "$TMP/$ARCHIVE.sha256sum"
(cd "$TMP" && if command -v sha256sum >/dev/null; then sha256sum -c "$ARCHIVE.sha256sum"; else shasum -a 256 -c "$ARCHIVE.sha256sum"; fi)
python3 - "$TMP/$ARCHIVE" "$DEST/cilium" <<'PY'
import shutil
import sys
import tarfile

archive, destination = sys.argv[1:]
with tarfile.open(archive, "r:gz") as source:
    members = [member for member in source.getmembers()
               if member.name == "cilium" and member.isfile()]
    if len(members) != 1:
        raise SystemExit("archive内のcilium実行ファイルを確認できません")
    extracted = source.extractfile(members[0])
    if extracted is None:
        raise SystemExit("cilium実行ファイルを展開できません")
    with open(destination, "wb") as output:
        shutil.copyfileobj(extracted, output)
PY
chmod 0755 "$DEST/cilium"
"$DEST/cilium" version --client
