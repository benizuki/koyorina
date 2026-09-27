#!/bin/bash
set -Eeuo pipefail

MODE=${1:-}
[[ $MODE == --execute || $MODE == --finish ]] || { echo "使用方法: $0 --execute|--finish" >&2; exit 2; }
ROOT=$(cd -- "$(dirname -- "$0")/../.." && pwd)
CONFIG_FILE=${APP_FORGE_AUDIT_CONFIG:-"$ROOT/contrib/observability/config.local.sh"}
[[ -r $CONFIG_FILE ]] || { echo "$CONFIG_FILEがありません。config.local.sh.exampleをコピーして設定してください" >&2; exit 2; }
set -a
source "$CONFIG_FILE"
set +a
: "${KUBE_CONTEXT:?config.local.shへKUBE_CONTEXTを設定してください}"
[[ $KUBE_CONTEXT == default ]] || { echo "開発クラスタ用にKUBE_CONTEXT=defaultを指定してください" >&2; exit 2; }
: "${SPLUNK_HEC_ENDPOINT:?config.local.shへSPLUNK_HEC_ENDPOINTを設定してください}"
: "${SPLUNK_HEC_TOKEN:?config.local.shへSPLUNK_HEC_TOKENを設定してください}"
: "${ORIGIN_CA_BUNDLE:?config.local.shへORIGIN_CA_BUNDLEを設定してください}"
[[ -r $ORIGIN_CA_BUNDLE ]] || { echo "ORIGIN_CA_BUNDLEを読み取れません" >&2; exit 2; }
command -v kubectl >/dev/null || { echo "kubectlがありません" >&2; exit 127; }
command -v ssh >/dev/null || { echo "sshがありません" >&2; exit 127; }
command -v openssl >/dev/null || { echo "opensslがありません。Rocky Linuxでは sudo dnf install -y openssl を実行してください" >&2; exit 127; }
source "$ROOT/contrib/observability/resolve-node-targets.sh"
"$ROOT/contrib/observability/configure-audit.sh" --check
echo "マスターノードのsudo認証を確認します。"
sudo -v
# The full connectivity suite can outlive sudo's timestamp. Refresh the existing
# credential without prompting while this migration process is running.
(while sleep 60; do sudo -n -v || exit; done) &
SUDO_KEEPALIVE_PID=$!
trap 'kill "$SUDO_KEEPALIVE_PID" 2>/dev/null || true' EXIT
trap 'rc=$?; echo "移行処理が${LINENO}行目で停止しました: ${BASH_COMMAND} (終了コード: $rc)" >&2; exit "$rc"' ERR
prepare_kubeconfig
K8S_API_HOST=$(kubectl --context "$KUBE_CONTEXT" get node "$SERVER_NODE" \
  -o 'jsonpath={.status.addresses[?(@.type=="InternalIP")].address}')
: "${K8S_API_HOST:?マスターノードのInternalIPを取得できません}"
for target in "${AGENT_SSH[@]}"; do
  if ! ssh "$target" "sudo -n true"; then
    echo "$targetではパスワードなしsudoが必要です。config.local.shのAGENT_SSHを確認してください。" >&2
    exit 2
  fi
done
CILIUM_CLI="$ROOT/.runtime/bin/cilium"
if [[ ! -x $CILIUM_CLI ]]; then "$ROOT/contrib/observability/install-cilium-cli.sh"; fi

RESUME=false
if sudo test -f /etc/rancher/k3s/config.yaml.d/90-cilium.yaml; then
  RESUME=true
  BACKUP=$(cat "$ROOT/.runtime/cilium-migration/latest" 2>/dev/null || true)
  [[ -n $BACKUP && -d $BACKUP ]] || {
    echo "再開に必要なバックアップ情報がありません: $ROOT/.runtime/cilium-migration/latest" >&2
    exit 2
  }
  echo "Flannel無効化後の状態を検出しました。既存バックアップを保持してCilium導入から再開します。"
fi

finish_migration() {
  echo "監査用ホストディレクトリを準備します。"
  run_server_root 'install -d -m 0755 /var/run/cilium/hubble /var/lib/koyorina-audit-otel; chcon -Rt container_file_t /var/run/cilium/hubble /var/lib/koyorina-audit-otel 2>/dev/null || true'
  for target in "${AGENT_SSH[@]}"; do
    run_ssh_root "$target" 'install -d -m 0755 /var/run/cilium/hubble /var/lib/koyorina-audit-otel; chcon -Rt container_file_t /var/run/cilium/hubble /var/lib/koyorina-audit-otel 2>/dev/null || true'
  done

  echo "TLS検査証明書と通信ポリシーを設定します。"
  "$ROOT/contrib/observability/configure-tls.sh"
  python3 "$ROOT/contrib/observability/render.py" cilium-policy.yaml --environment dev | kubectl --context "$KUBE_CONTEXT" apply -f -
  # The previously deployed legacy policy permits public TCP/443. Cilium unions
  # all matching egress allows, so remove it before the deny-by-default canary.
  # setup/manifest/apply.sh recreates the policy with the legacy-only selector.
  kubectl --context "$KUBE_CONTEXT" -n koyorina-codex delete networkpolicy \
    codex-agent-codex-egress --ignore-not-found

  echo "監査Collectorを配備します。"
  "$ROOT/contrib/observability/configure-audit.sh"
  if [ "$(python3 "$ROOT/setup/environments/load.py" dev --value AUDIT_LOG_EXPORTER)" != disabled ]; then
    kubectl --context "$KUBE_CONTEXT" -n koyorina-observability rollout status \
      daemonset/audit-collector --timeout=5m
  fi
  "$ROOT/contrib/observability/canary.sh"

  echo "Koyorinaを再開します。"
  ENV=dev KUBE_CONTEXT="$KUBE_CONTEXT" "$ROOT/setup/manifest/apply.sh"
  kubectl --context "$KUBE_CONTEXT" uncordon "${ALL_NODE_NAMES[@]}"
  kubectl --context "$KUBE_CONTEXT" get nodes
  echo "Ciliumへの移行が完了しました。バックアップ: $BACKUP"
}

if [[ $MODE == --finish ]]; then
  [[ $RESUME == true ]] || { echo "--finishはCilium導入済み環境だけで使用できます" >&2; exit 2; }
  finish_migration
  exit 0
fi

if [[ $RESUME == false ]]; then
# Never interrupt paid inference or an interactive login merely because maintenance began.
PROBE='import json,os,sys,urllib.request
r=urllib.request.Request("http://127.0.0.1:8080/runtime",headers={"Authorization":"Bearer "+os.environ["AGENT_TOKEN"]})
s=json.load(urllib.request.urlopen(r,timeout=15))
sys.exit(2 if s.get("busy") or s.get("login") else 0)'
for pod in $(kubectl --context "$KUBE_CONTEXT" -n koyorina-codex get pod -l app=koyorina-codex-agent -o name); do
  if ! kubectl --context "$KUBE_CONTEXT" -n koyorina-codex exec "$pod" -- python -c "$PROBE"; then
    echo "$podは処理中、または状態を確認できません。移行は開始していません" >&2
    exit 3
  fi
done

STAMP=$(date -u +%Y%m%dT%H%M%SZ)
BACKUP=${BACKUP_DIR:-"$ROOT/.runtime/cilium-migration/$STAMP"}
mkdir -p "$BACKUP"
chmod 700 "$BACKUP"
kubectl --context "$KUBE_CONTEXT" cluster-info >/dev/null
kubectl --context "$KUBE_CONTEXT" get nodes -o wide >"$BACKUP/nodes.txt"
kubectl --context "$KUBE_CONTEXT" get networkpolicy -A -o yaml >"$BACKUP/networkpolicies.yaml"
kubectl --context "$KUBE_CONTEXT" get pvc -A -o yaml >"$BACKUP/pvcs.yaml"
kubectl --context "$KUBE_CONTEXT" get deployments,statefulsets -A -o yaml >"$BACKUP/workloads.yaml"

NODE_BACKUP="mkdir -p /var/lib/koyorina-cilium-backup/$STAMP; cp -a /etc/rancher/k3s /var/lib/koyorina-cilium-backup/$STAMP/ 2>/dev/null || true; cp -a /etc/cni /var/lib/koyorina-cilium-backup/$STAMP/ 2>/dev/null || true; iptables-save > /var/lib/koyorina-cilium-backup/$STAMP/iptables.save; nft list ruleset > /var/lib/koyorina-cilium-backup/$STAMP/nft.rules 2>/dev/null || true"
run_server_root "$NODE_BACKUP"
for target in "${AGENT_SSH[@]}"; do
  run_ssh_root "$target" "$NODE_BACKUP"
done
printf '%s\n' "$BACKUP" >"$ROOT/.runtime/cilium-migration/latest"

kubectl --context "$KUBE_CONTEXT" cordon "${ALL_NODE_NAMES[@]}"
kubectl --context "$KUBE_CONTEXT" -n koyorina scale deployment/koyorina --replicas=0
kubectl --context "$KUBE_CONTEXT" -n koyorina-preview scale deployment/koyorina-preview-controller --replicas=0
kubectl --context "$KUBE_CONTEXT" -n koyorina-codex scale deployment/koyorina-codex-controller --replicas=0
kubectl --context "$KUBE_CONTEXT" -n koyorina-codex delete networkpolicy cilium-labelled-agent-flannel-egress --ignore-not-found
kubectl --context "$KUBE_CONTEXT" -n koyorina-codex delete pod -l app=koyorina-codex-agent --wait=true --timeout=120s || true
kubectl --context "$KUBE_CONTEXT" -n koyorina-preview delete pod -l app=koyorina-preview --wait=true --timeout=120s || true

for target in "${AGENT_SSH[@]}"; do ssh "$target" "sudo systemctl stop k3s-agent"; done
run_server_root 'mkdir -p /etc/rancher/k3s/config.yaml.d; printf "flannel-backend: none\ndisable-network-policy: true\n" > /etc/rancher/k3s/config.yaml.d/90-cilium.yaml; systemctl restart k3s'
until kubectl --context "$KUBE_CONTEXT" get --raw=/readyz >/dev/null 2>&1; do sleep 5; done
fi

# This cleanup is also required when resuming a failed migration. flannel.1
# retains UDP 8472 after k3s restarts and prevents cilium_vxlan from coming up.
run_server_root 'iptables-save | grep -v KUBE-ROUTER | iptables-restore; ip link delete flannel.1 2>/dev/null || true'
for target in "${AGENT_SSH[@]}"; do
  run_ssh_root "$target" 'iptables-save | grep -v KUBE-ROUTER | iptables-restore; ip link delete flannel.1 2>/dev/null || true'
done

if kubectl --context "$KUBE_CONTEXT" -n kube-system get configmap cilium-config >/dev/null 2>&1; then
  "$CILIUM_CLI" --context "$KUBE_CONTEXT" upgrade --version 1.20.2 \
    --values "$ROOT/contrib/observability/cilium-values.yaml" \
    --set k8sServiceHost="$K8S_API_HOST" --set k8sServicePort=6443
else
  "$CILIUM_CLI" --context "$KUBE_CONTEXT" install --version 1.20.2 \
    --values "$ROOT/contrib/observability/cilium-values.yaml" \
    --set k8sServiceHost="$K8S_API_HOST" --set k8sServicePort=6443
fi

# Cilium Operator registers the CRDs required by cilium-agent. The operator uses
# host networking, so let it run on the server before waiting for the agents.
# Keeping every node cordoned here creates a deadlock: the operator stays Pending
# while every cilium-agent waits for the missing CRDs.
kubectl --context "$KUBE_CONTEXT" uncordon "$SERVER_NODE"
kubectl --context "$KUBE_CONTEXT" -n kube-system rollout status deployment/cilium-operator --timeout=5m

for target in "${AGENT_SSH[@]}"; do ssh "$target" "sudo systemctl start k3s-agent"; done

kubectl --context "$KUBE_CONTEXT" -n kube-system rollout status daemonset/cilium --timeout=10m
kubectl --context "$KUBE_CONTEXT" -n kube-system rollout status daemonset/cilium-envoy --timeout=10m
kubectl --context "$KUBE_CONTEXT" uncordon "${ALL_NODE_NAMES[@]}"
kubectl --context "$KUBE_CONTEXT" -n kube-system rollout status deployment/hubble-relay --timeout=5m
kubectl --context "$KUBE_CONTEXT" -n kube-system rollout status deployment/hubble-ui --timeout=5m

# A resumed migration can retain restart counts and previous logs from the
# already-remediated Flannel/VXLAN conflict. Roll the agents once after cleanup
# so the connectivity test evaluates the current datapath state.
if [[ $RESUME == true ]]; then
  kubectl --context "$KUBE_CONTEXT" -n kube-system rollout restart daemonset/cilium
  kubectl --context "$KUBE_CONTEXT" -n kube-system rollout status daemonset/cilium --timeout=10m
fi

"$CILIUM_CLI" --context "$KUBE_CONTEXT" status --wait
CONNECTIVITY_MARKER="$BACKUP/connectivity-ok"
if [[ -f $CONNECTIVITY_MARKER ]]; then
  echo "このバックアップのCilium疎通試験は成功済みです。再実行を省略します。"
else
  "$CILIUM_CLI" --context "$KUBE_CONTEXT" connectivity test --log-check-only-test-time
  printf 'cilium=1.20.2 api=%s completed=%s\n' "$K8S_API_HOST" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >"$CONNECTIVITY_MARKER"
fi
"$CILIUM_CLI" --context "$KUBE_CONTEXT" connectivity test --cleanup
finish_migration
