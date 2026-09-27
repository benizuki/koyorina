#!/bin/sh
set -eu

command -v openssl >/dev/null 2>&1 || {
  echo "opensslがありません。Rocky Linuxでは sudo dnf install -y openssl を実行してください" >&2
  exit 127
}
command -v python3 >/dev/null 2>&1 || { echo "python3がありません" >&2; exit 127; }
command -v kubectl >/dev/null 2>&1 || { echo "kubectlがありません" >&2; exit 127; }

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
ENVIRONMENT=${ENV:-dev}
KUBE_CONTEXT=${KUBE_CONTEXT:-default}
RUNTIME=${AUDIT_CA_DIR:-"$ROOT/.runtime/network-audit"}
ORIGIN_CA_BUNDLE=${ORIGIN_CA_BUNDLE:-/etc/ssl/cert.pem}
: "${ORIGIN_CA_BUNDLE:?Envoy用の公開CA bundleをORIGIN_CA_BUNDLEへ指定してください}"
[ -r "$ORIGIN_CA_BUNDLE" ] || { echo "接続先検証用のCA bundleを読み取れません" >&2; exit 2; }
umask 077
mkdir -p "$RUNTIME"
ROOT_KEY="$RUNTIME/root-ca.key"
ROOT_CERT="$RUNTIME/root-ca.crt"
LEAF_KEY="$RUNTIME/destinations.key"
LEAF_CERT="$RUNTIME/destinations.crt"
VERTEX_HOST=$(python3 "$ROOT/setup/environments/load.py" "$ENVIRONMENT" --value VERTEX_API_HOST)
# 社内パッケージプロキシを使わない場合は空のままでよい（DNS.7/8 は末尾に付くだけ）。
NPM_PROXY_HOST=${NPM_PROXY_HOST:-}
PYPI_PROXY_HOST=${PYPI_PROXY_HOST:-}

if [ ! -s "$ROOT_KEY" ]; then
  openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:4096 \
    -out "$ROOT_KEY" >/dev/null 2>&1
fi
# OpenSSL 3 rejects a signing certificate that merely has CA:TRUE implied by
# being self-signed. Keep the offline key, but repair certificates created by
# the first migration script which omitted basicConstraints/keyUsage.
if [ ! -s "$ROOT_CERT" ] || \
   ! openssl x509 -in "$ROOT_CERT" -noout -text 2>/dev/null | grep -q 'CA:TRUE' || \
   ! openssl x509 -in "$ROOT_CERT" -noout -text 2>/dev/null | grep -q 'Certificate Sign'; then
  openssl req -x509 -new -key "$ROOT_KEY" -sha256 -days 3650 \
    -subj '/CN=Koyorina Development Audit CA' \
    -addext 'basicConstraints=critical,CA:TRUE,pathlen:0' \
    -addext 'keyUsage=critical,keyCertSign,cRLSign' \
    -addext 'subjectKeyIdentifier=hash' \
    -out "$ROOT_CERT" >/dev/null 2>&1
fi
cat >"$RUNTIME/leaf.cnf" <<EOF
[req]
prompt=no
distinguished_name=dn
req_extensions=ext
[dn]
CN=Koyorina TLS inspection
[ext]
subjectAltName=@names
basicConstraints=critical,CA:FALSE
keyUsage=critical,digitalSignature,keyEncipherment
extendedKeyUsage=serverAuth
[names]
DNS.1=openai.com
DNS.2=*.openai.com
DNS.3=chatgpt.com
DNS.4=*.chatgpt.com
DNS.5=$VERTEX_HOST
DNS.6=oauth2.googleapis.com
$( [ -n "$NPM_PROXY_HOST" ] && echo "DNS.7=$NPM_PROXY_HOST" )
$( [ -n "$PYPI_PROXY_HOST" ] && echo "DNS.8=$PYPI_PROXY_HOST" )
EOF
openssl req -new -newkey rsa:3072 -nodes -keyout "$LEAF_KEY" -out "$RUNTIME/leaf.csr" \
  -config "$RUNTIME/leaf.cnf" >/dev/null 2>&1
openssl x509 -req -sha256 -days 90 -in "$RUNTIME/leaf.csr" -CA "$ROOT_CERT" -CAkey "$ROOT_KEY" \
  -CAcreateserial -out "$LEAF_CERT" -extensions ext -extfile "$RUNTIME/leaf.cnf" >/dev/null 2>&1

kubectl --context "$KUBE_CONTEXT" create namespace cilium-secrets --dry-run=client -o yaml | kubectl --context "$KUBE_CONTEXT" apply -f -
kubectl --context "$KUBE_CONTEXT" -n koyorina-codex create configmap koyorina-audit-ca \
  --from-file=ca.crt="$ROOT_CERT" --dry-run=client -o yaml | kubectl --context "$KUBE_CONTEXT" apply -f - >/dev/null
kubectl --context "$KUBE_CONTEXT" -n cilium-secrets create secret tls koyorina-audit-destinations \
  --cert="$LEAF_CERT" --key="$LEAF_KEY" --dry-run=client -o yaml | kubectl --context "$KUBE_CONTEXT" apply -f - >/dev/null
kubectl --context "$KUBE_CONTEXT" -n cilium-secrets create secret generic koyorina-audit-origin-roots \
  --from-file=ca.crt="$ORIGIN_CA_BUNDLE" --dry-run=client -o yaml | \
  kubectl --context "$KUBE_CONTEXT" apply --server-side \
    --field-manager=koyorina-network-audit -f - >/dev/null
echo "TLS検査用証明書を設定しました。オフラインCA秘密鍵の保存先: $ROOT_KEY"
