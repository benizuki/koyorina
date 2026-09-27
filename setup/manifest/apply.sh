#!/bin/sh
# 本番へ反映する。管理アプリ・プレビュー・生成の3か所へまとめて当てる。
#
# 1か所ずつ当てていると、必ずどこかが古いまま残る。実際、プレビューの
# コントローラだけ数版古いイメージで動き、直したはずの不具合が再現した。
#
# 使い方: setup/manifest/apply.sh [agentのイメージ]
#         ENV=prod setup/manifest/apply.sh [agentのイメージ]
# agentを省略すると、いまクラスタに入っているものを引き継ぐ。
# ENVを省略するとdev。ドメインとイメージの置き場は setup/environments/ を見る。
set -eu

cd "$(dirname "$0")/../.."

ENV="${ENV:-dev}"
# 当てる先はKUBE_CONTEXTで選ぶ。別の環境へ持っていくとき、ここを見落とすと
# 手元のクラスタへ本番の設定を当ててしまう。既定は開発。
KUBECTL="kubectl --context ${KUBE_CONTEXT:-default}"

# マニフェストはテンプレート。環境の値で埋めたものを当てる。
# 当てたものを残すため、消さずに名前を出す。何を当てたか後から確かめられる。
RENDERED="$(mktemp -d)"
uv run python setup/manifest/render.py --all --out "$RENDERED" --environment "$ENV" >/dev/null

REGISTRY="$(uv run python setup/environments/load.py "$ENV" --value REGISTRY)"
DOMAIN="$(uv run python setup/environments/load.py "$ENV" --value DOMAIN)"
APP_NAME="$(uv run python setup/environments/load.py "$ENV" --value APP_NAME)"
GEMINI_API_BACKEND="$(uv run python setup/environments/load.py "$ENV" --value GEMINI_API_BACKEND)"
APP="$(grep -m1 -o "$(printf '%s' "$REGISTRY" | sed 's/\./\\./g')/$(printf '%s' "$APP_NAME" | sed 's/\./\\./g')@sha256:[0-9a-f]\{64\}" \
  "$RENDERED/app.yaml")"
# agentもマニフェストが持つ。以前はクラスタにいま入っているものを引き継いでいたため、
# 何度当てても agent だけ古いままだった（引数を書かない限り更新されなかった）。
AGENT="${1:-$(grep -m1 -o "$(printf '%s' "$REGISTRY" | sed 's/\./\\./g')/$(printf '%s' "$APP_NAME" | sed 's/\./\\./g')-agent@sha256:[0-9a-f]\{64\}" \
  "$RENDERED/codex-controller.yaml")}"

printf 'env:     %s\ncontext: %s\ndomain:  %s\napp:     %s\nagent:   %s\nfiles:   %s\n' \
  "$ENV" "${KUBE_CONTEXT:-default}" "$DOMAIN" "$APP" "$AGENT" "$RENDERED"

# 当てるのはマニフェストに書いてあるdigest。pushしたあと make pin を忘れると、
# 前と同じイメージを当て直すだけで、成功したように見えて何も変わらない。
# 黙って進むと原因に気づけないので、違っていたらここで知らせる。
TAG="$(git rev-parse --short=12 HEAD 2>/dev/null || true)"
if [ -n "$TAG" ]; then
  PUSHED="$(docker buildx imagetools inspect "$REGISTRY/$APP_NAME:$TAG" \
    --format '{{.Manifest.Digest}}' 2>/dev/null || true)"
  case "$APP" in
    *"$PUSHED") ;;
    *) printf '\n注意: push済みの %s は %s です。\n      これを当てるなら make pin を実行してから、もう一度このスクリプトを動かしてください。\n\n' \
         "$TAG" "$PUSHED" ;;
  esac
fi

# app.yamlにはpreview/codex namespaceのRoleもあるため、先に各namespaceと
# サービス間トークンを用意する。新規クラスタでも途中まで適用されない順序にする。
$KUBECTL apply -f "$RENDERED/namespace.yaml"

# configure-preview.py が preview.yaml を適用し、Controllerと管理アプリの
# サービス間トークンも同時に用意する。
uv run python setup/manifest/configure-preview.py --environment "$ENV"

# yamlなどの依存はプロジェクトの環境にある。素のpython3では動かない。
uv run python setup/manifest/configure-codex.py --app-image "$APP" --agent-image "$AGENT" \
  --environment "$ENV"

# Developer APIを使う場合は、codex namespaceを作った後、アプリを起動する前に
# APIキーを両namespaceへ保存する。未指定なら既存Secretをそのまま使う。
if [ -n "${GEMINI_API_KEY:-}" ]; then
  uv run python setup/manifest/configure-gemini-api-key.py --environment "$ENV"
elif [ "$GEMINI_API_BACKEND" = developer ] && \
     ! $KUBECTL -n "$APP_NAME" get secret "$APP_NAME-gemini-api" >/dev/null 2>&1; then
  # 配備は止めない。キーは画面（マスター管理 → システム設定 → 生成AIの設定）でも入れられる。
  printf '%s\n' '注意: Gemini API のキーが未登録です。配備後に画面（システム設定 → 生成AIの設定）で設定してください。' >&2
fi

# 新しいアプリは新しいDB列を前提にする。先に切り替えると、ログイン直後の
# 一覧取得が500になり、画面に接続エラーが残る。毎回一意なJobで未適用分だけを
# 実行し、完了を確認してからDeploymentを更新する。
MIGRATION_JOB="$($KUBECTL create -f "$RENDERED/migrate.yaml" -o name)"
if ! $KUBECTL -n "$APP_NAME" wait --for=condition=complete "$MIGRATION_JOB" --timeout=180s; then
  $KUBECTL -n "$APP_NAME" logs "$MIGRATION_JOB" --all-containers=true || true
  printf '%s\n' 'DBマイグレーションに失敗したため、アプリの更新を中止しました。' >&2
  exit 1
fi

$KUBECTL apply -f "$RENDERED/app.yaml"

# ConfigMapやSecretの内容だけが変わってもDeploymentのPod templateは変わらないため、
# 既存Podは古い環境変数を持ったままになる。3つの常駐Deploymentを明示的に再起動し、
# 過去の起動失敗で残ったProgressDeadlineExceededも新しい世代へ更新する。
$KUBECTL -n "$APP_NAME" rollout restart "deployment/$APP_NAME"
$KUBECTL -n "$APP_NAME-preview" rollout restart "deployment/$APP_NAME-preview-controller"
$KUBECTL -n "$APP_NAME-codex" rollout restart "deployment/$APP_NAME-codex-controller"

$KUBECTL -n "$APP_NAME" rollout status "deploy/$APP_NAME" --timeout=300s
$KUBECTL -n "$APP_NAME-preview" rollout status "deploy/$APP_NAME-preview-controller" --timeout=300s
$KUBECTL -n "$APP_NAME-codex" rollout status "deploy/$APP_NAME-codex-controller" --timeout=300s
