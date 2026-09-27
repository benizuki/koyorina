# 開発環境の生成AI通信監査

既存の開発用k3sクラスタをFlannelからCiliumへ移行し、生成AI通信をHubbleで監査する
ためのスクリプトです。新しくAnsibleでCiliumクラスタを構築する手順ではありません。

主な動作は次のとおりです。

- `koyorina-codex` namespaceから公開HTTPSへの通信を許可
- プライベートネットワーク、link-local、メタデータIPへの通信を拒否
- HubbleとEnvoyで通信を記録し、OpenTelemetry CollectorからSplunkへ転送
- プロンプト、添付ファイル、生成コード、HTTP body・header・queryは外部ログへ送らない

転送するのは接続開始・終了、DNS問い合わせ、拒否・エラーなどの監査イベントです。
ローカルには障害調査用のHubbleフローが残ります。TLS検査用CAの秘密鍵は
`.runtime/network-audit`に保存され、Kubernetesには登録されません。秘密鍵は別の安全な場所へ
バックアップし、証明書の期限前に`configure-tls.sh`で更新してください。

## 前提

- クラスタを停止できるメンテナンス時間がある
- serverノードに`kubectl`、OpenSSL、curl、Python 3がある
- serverノードから各agentへSSH接続でき、agent側でパスワードなし`sudo`を使える
- Splunk HECのendpoint、token、TLS検証用CAを用意している
- Koyorinaのイメージをbuild・pushし、マニフェストをdigest固定している

以下のコマンドはserverノード上のリポジトリルートで実行します。

## 1. 設定する

```sh
cp contrib/observability/config.local.sh.example \
  contrib/observability/config.local.sh

chmod 600 contrib/observability/config.local.sh
```

`config.local.sh`へ次の値を設定します。

- server/agentのノード名とagentへのSSH接続先
- Splunk HECのendpointとtoken
- Splunkと外部接続先を検証するCA bundle

HTTPのSplunk HECを使う場合は`SPLUNK_HEC_ALLOW_HTTP=true`が必要です。tokenと監査イベントが
暗号化されないため、閉域ネットワークでのみ使用してください。

## 2. 接続を確認する

```sh
ssh <agent-1> sudo -n true
ssh <agent-2> sudo -n true
test -r <ORIGIN_CA_BUNDLEに設定したファイル>
```

SSHユーザーが異なる場合は、`AGENT_SSH`を`user@host`形式で指定します。

## 3. Ciliumへ移行する

```sh
contrib/observability/migrate-dev-cilium.sh --execute
```

スクリプトは事前検査とバックアップを行ってからワークロードを停止し、Cilium、Hubble、
通信ポリシー、TLS検査、Collectorを設定します。PVCのデータは変更しません。

Ciliumの導入後、TLSやCollectorの設定で停止した場合は後半だけ再開できます。

```sh
contrib/observability/migrate-dev-cilium.sh --finish
```

## 4. 確認する

```sh
export KUBECONFIG="$HOME/.kube/config"

kubectl -n kube-system get pods -l k8s-app=cilium
kubectl -n kube-system get pods -l k8s-app=hubble-relay
kubectl -n koyorina-observability rollout status daemonset/audit-collector

contrib/observability/canary.sh
```

SplunkのDashboardとSaved Searchは[Splunkの手順](splunk/README.md)で追加します。監査ログに
token、cookie、prompt、生成コード、HTTP bodyが含まれていないことも確認してください。

## 外部ログ転送を止める

環境ファイルの`AUDIT_LOG_EXPORTER=disabled`を設定して反映します。Hubbleのローカルログは
残ります。

```sh
contrib/observability/configure-audit.sh
```

## Flannelへ戻す

最新のバックアップを使ってロールバックします。PVCは変更しません。

```sh
contrib/observability/rollback-dev-cilium.sh --execute
```
