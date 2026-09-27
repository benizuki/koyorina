#!/bin/sh
# Helmチャートをgitタグ由来のバージョンでパッケージする。
#
# Chart.yaml自体には手で編集するバージョンを持たせない（タグを打つたびに
# 書き換える運用は忘れやすい）。git describeの値をそのまま渡す。
#
#   タグが打ってあるコミット:        v0.1.0        -> version 0.1.0
#   タグから何コミットか進んだ状態:  v0.1.0-3-gabcdef -> version 0.1.0-3-gabcdef
#   タグが一つも無いリポジトリ:      (該当なし)    -> version 0.0.0+<コミットhash>
#   作業ツリーに未コミットの変更がある場合は末尾に -dirty が付く。
#
# 使い方: setup/helm/package.sh [出力先ディレクトリ（既定: dist）]
set -eu

cd "$(dirname "$0")/../.."
OUT="${1:-dist}"
mkdir -p "$OUT"

if RAW="$(git describe --tags --dirty 2>/dev/null)"; then
  VERSION="${RAW#v}"
else
  # タグが一つも無い。semverの必須形式(X.Y.Z)を満たしつつ追跡できるよう、
  # コミットhashをビルドメタデータとして付ける。
  HASH="$(git rev-parse --short HEAD)"
  DIRTY=""
  git diff --quiet --ignore-submodules HEAD -- || DIRTY="-dirty"
  VERSION="0.0.0+${HASH}${DIRTY}"
fi

echo "packaging koyorina chart as ${VERSION}" >&2
helm package --version "$VERSION" --app-version "$VERSION" setup/helm/koyorina -d "$OUT"
