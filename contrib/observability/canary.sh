#!/bin/sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
KUBE_CONTEXT=${KUBE_CONTEXT:-default}
IMAGE=$(python3 "$ROOT/setup/manifest/render.py" codex-controller.yaml | \
  python3 -c 'import sys,yaml; ds=list(yaml.safe_load_all(sys.stdin)); print(next(x for x in ds if x and x.get("kind")=="ConfigMap")["data"]["CONTROLLER_AGENT_IMAGE"])')
cleanup() { kubectl --context "$KUBE_CONTEXT" -n koyorina-codex delete pod network-audit-canary --ignore-not-found --wait=false >/dev/null 2>&1 || true; }
trap cleanup EXIT
cleanup
cat <<EOF | kubectl --context "$KUBE_CONTEXT" apply -f - >/dev/null
apiVersion: v1
kind: Pod
metadata:
  name: network-audit-canary
  namespace: koyorina-codex
  labels: {app: koyorina-codex-agent, forge-route: shared, forge-network-policy: cilium}
spec:
  restartPolicy: Never
  containers:
    - name: canary
      image: $IMAGE
      command: [sleep, "600"]
      env:
        - {name: SSL_CERT_FILE, value: /run/koyorina-audit/ca.crt}
      volumeMounts:
        - {name: audit-ca, mountPath: /run/koyorina-audit, readOnly: true}
      resources:
        requests: {cpu: 10m, memory: 32Mi}
        limits: {cpu: 100m, memory: 128Mi}
      securityContext: {runAsNonRoot: true, runAsUser: 10001, runAsGroup: 10001, allowPrivilegeEscalation: false, capabilities: {drop: [ALL]}, seccompProfile: {type: RuntimeDefault}}
  volumes:
    - {name: audit-ca, configMap: {name: koyorina-audit-ca, defaultMode: 0444}}
EOF
kubectl --context "$KUBE_CONTEXT" -n koyorina-codex wait pod/network-audit-canary --for=condition=Ready --timeout=3m
tls_probe='import socket,ssl,sys
host=sys.argv[1]
try:
 raw=socket.create_connection((host,443),5)
 with ssl.create_default_context().wrap_socket(raw,server_hostname=host): pass
 result="open"
except Exception as error:
 print(type(error).__name__,str(error)[:200]); result="blocked"
print(host,result)
raise SystemExit(0 if result=="open" else 1)'
probe='import socket,sys
host=sys.argv[1]; port=int(sys.argv[2]); expected=sys.argv[3]
try:
 s=socket.create_connection((host,port),5); s.close(); result="open"
except Exception: result="blocked"
print(host,result)
raise SystemExit(0 if result==expected else 1)'
kubectl --context "$KUBE_CONTEXT" -n koyorina-codex exec network-audit-canary -- python -c "$tls_probe" openai.com
kubectl --context "$KUBE_CONTEXT" -n koyorina-codex exec network-audit-canary -- python -c "$probe" example.com 443 open
kubectl --context "$KUBE_CONTEXT" -n koyorina-codex exec network-audit-canary -- python -c "$probe" 1.1.1.1 443 open
kubectl --context "$KUBE_CONTEXT" -n koyorina-codex exec network-audit-canary -- python -c "$probe" 169.254.169.254 80 blocked
kubectl --context "$KUBE_CONTEXT" -n koyorina-codex exec network-audit-canary -- python -c "$probe" 10-0-0-1.nip.io 443 blocked
