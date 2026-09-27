#!/bin/sh
# 利用者ごとのPVCにある作業場所を、アプリ単位の共有PVCへまとめて移す。
#
# PVCは利用者ごとに別のボリュームで、Jobは名前を書いたPVCしかマウントできない。
# だから「全部いっぺんに」は書けず、PVCの数だけJobを流すことになる。
# その繰り返しをここで回す。手で1人ずつ流しても結果は同じ。
#
# 使い方:
#   CONFIRM=1 sh setup/manifest/migrate-workspaces.sh
#   KUBECTL="sudo k3s kubectl" ENV=prod CONFIRM=1 sh setup/manifest/migrate-workspaces.sh
set -eu

cd "$(dirname "$0")/../.."
ENV="${ENV:-dev}"
NAMESPACE=koyorina-codex
JOB=koyorina-migrate-workspaces
# GCEのサーバVMでは k3s 同梱の kubectl を使う。KUBECTL で丸ごと差し替えられる。
#   KUBECTL="sudo k3s kubectl" ENV=prod CONFIRM=1 sh setup/manifest/migrate-workspaces.sh
KUBECTL="${KUBECTL:-kubectl --context ${KUBE_CONTEXT:-default}} -n $NAMESPACE"

claims="$($KUBECTL get pvc -o name | sed 's|persistentvolumeclaim/||' | grep '^codex-' || true)"
if [ -z "$claims" ]; then
  echo "移す対象の利用者PVCがありません。すでに済んでいる可能性があります。"
  exit 0
fi

printf '移す対象:\n%s\n\n' "$claims"
# 書き込み中のファイルを移すと壊れる。止まっているかは人にしか判断できない。
$KUBECTL get pods -l app=koyorina-codex-agent 2>/dev/null || true
if [ "${CONFIRM:-}" != "1" ]; then
  echo
  echo "生成が走っていないことを確かめてから CONFIRM=1 を付けて実行してください。" >&2
  exit 1
fi

for claim in $claims; do
  echo "==> $claim"
  # 同じ名前のJobは作り直せない（apply しても unchanged になり、前のPVCを見続ける）。
  # 前回の残りがあれば先に消す。
  $KUBECTL delete job "$JOB" --ignore-not-found --wait=true
  python3 setup/manifest/render.py migrate-workspaces.yaml --environment "$ENV" \
    | sed "s/codex-REPLACE_WITH_USER_HEX/$claim/" \
    | $KUBECTL apply -f -
  # Podを作れない種類の失敗（PodSecurityなど）ではJobは失敗にならず、作成を
  # 再試行し続ける。待つだけだと理由が出ないので、切れたらイベントを見せる。
  if ! $KUBECTL wait --for=condition=complete --timeout=600s "job/$JOB"; then
    $KUBECTL logs "job/$JOB" 2>/dev/null || true
    echo "--- Jobのイベント ---" >&2
    $KUBECTL describe job "$JOB" | sed -n '/Events:/,$p' >&2 || true
    echo "$claim の移行に失敗しました。ここで中断します。" >&2
    exit 1
  fi
  $KUBECTL logs "job/$JOB"
  # 名前が同じJobは作り直せない。次の利用者へ進む前に消す。
  $KUBECTL delete job "$JOB"
done

echo
echo "完了しました。sh setup/manifest/apply.sh で生成Podを入れ替えてください。"
