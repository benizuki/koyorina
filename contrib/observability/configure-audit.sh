#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
ENVIRONMENT=${ENV:-dev}
KUBE_CONTEXT=${KUBE_CONTEXT:-default}
EXPORTER=$(python3 "$ROOT/setup/environments/load.py" "$ENVIRONMENT" --value AUDIT_LOG_EXPORTER)

if [ "$EXPORTER" = disabled ]; then
  # 外部転送を止める環境ではCollector自体を停止し、HEC/Cloud Logging向けの
  # egressポリシーも残さない。次回 splunk_hec/googlecloud に戻せば再作成される。
  kubectl --context "$KUBE_CONTEXT" create namespace koyorina-observability \
    --dry-run=client -o yaml | kubectl --context "$KUBE_CONTEXT" apply -f - >/dev/null
  if [ "${1:-}" != --check ]; then
    kubectl --context "$KUBE_CONTEXT" -n koyorina-observability \
      delete daemonset audit-collector --ignore-not-found
    kubectl --context "$KUBE_CONTEXT" -n koyorina-observability \
      delete ciliumnetworkpolicy audit-collector-egress --ignore-not-found
  fi
  exit 0
elif [ "$EXPORTER" = splunk_hec ]; then
  : "${SPLUNK_HEC_ENDPOINT:?SPLUNK_HEC_ENDPOINTを設定してください}"
  : "${SPLUNK_HEC_TOKEN:?SPLUNK_HEC_TOKENを設定してください}"
  HEC_SCHEME=$(python3 -c 'import sys, urllib.parse as u; p=u.urlparse(sys.argv[1]); assert p.scheme in {"http","https"} and p.hostname and not p.username and not p.password and not p.query and not p.fragment; print(p.scheme)' "$SPLUNK_HEC_ENDPOINT")
  HEC_HOST=$(python3 -c 'import sys, urllib.parse as u; print(u.urlparse(sys.argv[1]).hostname)' "$SPLUNK_HEC_ENDPOINT")
  HEC_PORT=$(python3 -c 'import sys, urllib.parse as u; p=u.urlparse(sys.argv[1]); print(p.port or (443 if p.scheme == "https" else 80))' "$SPLUNK_HEC_ENDPOINT")
  if [ "$HEC_SCHEME" = http ]; then
    [ "${SPLUNK_HEC_ALLOW_HTTP:-false}" = true ] || {
      echo "HTTPのSplunk HECを使うにはSPLUNK_HEC_ALLOW_HTTP=trueが必要です" >&2
      exit 2
    }
    : "${ORIGIN_CA_BUNDLE:?HTTP構成でもCollector設定検証用にORIGIN_CA_BUNDLEを指定してください}"
    [ -r "$ORIGIN_CA_BUNDLE" ] || { echo "ORIGIN_CA_BUNDLEを読み取れません" >&2; exit 2; }
    SPLUNK_HEC_CA_FILE=$ORIGIN_CA_BUNDLE
  else
    : "${SPLUNK_HEC_CA_FILE:?HTTPSではSPLUNK_HEC_CA_FILEを指定してください}"
    [ -r "$SPLUNK_HEC_CA_FILE" ] || { echo "SplunkのCAファイルを読み取れません" >&2; exit 2; }
  fi
  if HEC_CIDR=$(python3 -c 'import ipaddress,sys; a=ipaddress.ip_address(sys.argv[1]); print(f"{a}/{a.max_prefixlen}")' "$HEC_HOST" 2>/dev/null); then
    HEC_DESTINATION="toCIDR: [\"$HEC_CIDR\"]"
  else
    HEC_DESTINATION="toFQDNs: [{matchName: \"$HEC_HOST\"}]"
  fi
  [ "${1:-}" = --check ] && exit 0
  INDEX=$(python3 "$ROOT/setup/environments/load.py" "$ENVIRONMENT" --value AUDIT_SPLUNK_INDEX)
  kubectl --context "$KUBE_CONTEXT" create namespace koyorina-observability --dry-run=client -o yaml | kubectl --context "$KUBE_CONTEXT" apply -f -
  export KUBE_CONTEXT SPLUNK_HEC_ENDPOINT SPLUNK_HEC_TOKEN INDEX HEC_SCHEME
  export SPLUNK_HEC_CA_FILE
  python3 - <<'PY'
import json, os, subprocess
base = ["kubectl", "--context", os.environ["KUBE_CONTEXT"], "-n", "koyorina-observability"]
read = subprocess.run([*base, "get", "secret", "splunk-hec", "-o", "json"],
                      text=True, capture_output=True)
metadata = {"name": "splunk-hec", "namespace": "koyorina-observability"}
verb = "create"
if read.returncode == 0:
    metadata["resourceVersion"] = json.loads(read.stdout)["metadata"]["resourceVersion"]
    verb = "replace"
data = {"endpoint": os.environ["SPLUNK_HEC_ENDPOINT"],
        "token": os.environ["SPLUNK_HEC_TOKEN"],
        "index": os.environ["INDEX"],
        "allow-http": "true" if os.environ["HEC_SCHEME"] == "http" else "false"}
data["ca.crt"] = open(os.environ["SPLUNK_HEC_CA_FILE"]).read()
document = {"apiVersion": "v1", "kind": "Secret", "type": "Opaque", "metadata": metadata,
            "stringData": data}
subprocess.run([*base, verb, "-f", "-"], input=json.dumps(document), text=True,
               check=True, stdout=subprocess.DEVNULL)
PY
else
  HEC_HOST=oauth2.googleapis.com
  HEC_PORT=443
  HEC_DESTINATION='toFQDNs: [{matchName: "oauth2.googleapis.com"}]'
  [ "${1:-}" = --check ] && exit 0
fi

cat <<EOF | kubectl --context "$KUBE_CONTEXT" apply -f -
apiVersion: cilium.io/v2
kind: CiliumNetworkPolicy
metadata: {name: audit-collector-egress, namespace: koyorina-observability}
spec:
  endpointSelector: {matchLabels: {app: audit-collector}}
  ingress:
    - fromEntities: [host, remote-node]
      toPorts: [{ports: [{port: "13133", protocol: TCP}, {port: "8081", protocol: TCP}]}]
  egress:
    - toEndpoints:
        - matchLabels: {k8s:io.kubernetes.pod.namespace: kube-system, k8s:k8s-app: kube-dns}
      toPorts:
        - ports: [{port: "53", protocol: ANY}]
          rules: {dns: [{matchPattern: "*"}]}
    - $HEC_DESTINATION
      toPorts: [{ports: [{port: "$HEC_PORT", protocol: TCP}]}]
EOF

python3 "$ROOT/contrib/observability/render.py" collector.yaml --environment "$ENVIRONMENT" | \
  kubectl --context "$KUBE_CONTEXT" apply -f -
# ConfigMap volumes update on disk, but the Collector does not hot-reload its
# configuration. Restart explicitly so a repaired parser/exporter is actually used.
kubectl --context "$KUBE_CONTEXT" -n koyorina-observability rollout restart \
  daemonset/audit-collector
