#!/bin/bash
# config.local.shの固定値を、移行とロールバックで共通利用する。

: "${SERVER_NODE:?config.local.shへSERVER_NODEを設定してください}"
declare -p AGENT_NODES AGENT_SSH >/dev/null 2>&1 || {
  echo "config.local.shへAGENT_NODESとAGENT_SSHを設定してください" >&2
  exit 2
}
[[ $(hostname -s) == "$SERVER_NODE" ]] || {
  echo "このスクリプトは$SERVER_NODE上で実行してください" >&2
  exit 2
}
[[ ${#AGENT_NODES[@]} -eq ${#AGENT_SSH[@]} ]] || {
  echo "AGENT_NODESとAGENT_SSHは同じ件数にしてください" >&2
  exit 2
}

ALL_NODE_NAMES=("$SERVER_NODE" "${AGENT_NODES[@]}")

run_ssh_root() {
  local target=$1 command=$2 quoted
  printf -v quoted '%q' "$command"
  ssh "$target" "sudo sh -c $quoted"
}

run_server_root() {
  sudo sh -c "$1"
}

prepare_kubeconfig() {
  KUBECONFIG="$HOME/.kube/config"
  export KUBECONFIG
  mkdir -p "$(dirname "$KUBECONFIG")"
  chmod 0700 "$(dirname "$KUBECONFIG")"
  sudo install -m 0600 -o "$(id -u)" -g "$(id -g)" \
    /etc/rancher/k3s/k3s.yaml "$KUBECONFIG"
}
