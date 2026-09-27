#!/bin/bash
set -euo pipefail

[[ ${1:-} == --execute ]] || { echo "使用方法: $0 --execute [バックアップのタイムスタンプ]" >&2; exit 2; }
ROOT=$(cd -- "$(dirname -- "$0")/../.." && pwd)
CONFIG_FILE=${APP_FORGE_AUDIT_CONFIG:-"$ROOT/contrib/observability/config.local.sh"}
[[ -r $CONFIG_FILE ]] || { echo "$CONFIG_FILEがありません。config.local.sh.exampleをコピーして設定してください" >&2; exit 2; }
source "$CONFIG_FILE"
: "${KUBE_CONTEXT:?config.local.shへKUBE_CONTEXTを設定してください}"
[[ $KUBE_CONTEXT == default ]] || { echo "ロールバックは開発環境だけで実行できます" >&2; exit 2; }
STAMP=${2:-$(basename "$(cat "$ROOT/.runtime/cilium-migration/latest")")}
source "$ROOT/contrib/observability/resolve-node-targets.sh"
echo "マスターノードのsudo認証を確認します。"
sudo -v
prepare_kubeconfig
CILIUM_CLI="$ROOT/.runtime/bin/cilium"
[[ -x $CILIUM_CLI ]] || { echo "Cilium CLIがありません: $CILIUM_CLI" >&2; exit 2; }

kubectl --context "$KUBE_CONTEXT" cordon "${ALL_NODE_NAMES[@]}" || true
kubectl --context "$KUBE_CONTEXT" -n koyorina scale deployment/koyorina --replicas=0 || true
kubectl --context "$KUBE_CONTEXT" -n koyorina-preview scale deployment/koyorina-preview-controller --replicas=0 || true
kubectl --context "$KUBE_CONTEXT" -n koyorina-codex scale deployment/koyorina-codex-controller --replicas=0 || true
"$CILIUM_CLI" --context "$KUBE_CONTEXT" uninstall --wait || true
for target in "${AGENT_SSH[@]}"; do ssh "$target" "sudo systemctl stop k3s-agent"; done
NODE_ROLLBACK="test -d /var/lib/koyorina-cilium-backup/$STAMP; ip link delete cilium_host 2>/dev/null || true; ip link delete cilium_net 2>/dev/null || true; ip link delete cilium_vxlan 2>/dev/null || true; iptables-save | grep -v CILIUM_ | iptables-restore; rm -rf /var/run/cilium /var/lib/cilium; rm -f /etc/rancher/k3s/config.yaml.d/90-cilium.yaml; cp -a /var/lib/koyorina-cilium-backup/$STAMP/k3s/. /etc/rancher/k3s/"
run_server_root "$NODE_ROLLBACK"
for target in "${AGENT_SSH[@]}"; do
  run_ssh_root "$target" "$NODE_ROLLBACK"
done
run_server_root 'systemctl restart k3s'
until kubectl --context "$KUBE_CONTEXT" get --raw=/readyz >/dev/null 2>&1; do sleep 5; done
for target in "${AGENT_SSH[@]}"; do ssh "$target" "sudo systemctl start k3s-agent"; done
kubectl --context "$KUBE_CONTEXT" wait node --all --for=condition=Ready --timeout=10m
kubectl --context "$KUBE_CONTEXT" apply -f "$ROOT/contrib/observability/flannel-rollback-policy.yaml"
ENV=dev KUBE_CONTEXT="$KUBE_CONTEXT" "$ROOT/setup/manifest/apply.sh"
kubectl --context "$KUBE_CONTEXT" -n koyorina-codex patch configmap koyorina-codex-controller \
  --type merge -p '{"data":{"CONTROLLER_AUDIT_CA_CONFIGMAP":""}}'
kubectl --context "$KUBE_CONTEXT" -n koyorina-codex rollout restart deployment/koyorina-codex-controller
kubectl --context "$KUBE_CONTEXT" -n koyorina-codex rollout status deployment/koyorina-codex-controller --timeout=3m
kubectl --context "$KUBE_CONTEXT" -n koyorina-codex delete pod -l app=koyorina-codex-agent --ignore-not-found --wait=true
kubectl --context "$KUBE_CONTEXT" uncordon "${ALL_NODE_NAMES[@]}"
kubectl --context "$KUBE_CONTEXT" -n kube-system rollout status deployment/coredns --timeout=3m
kubectl --context "$KUBE_CONTEXT" get pvc -A
echo "Flannelと保存済みのk3s設定を復元しました。PVCは変更していません。"
