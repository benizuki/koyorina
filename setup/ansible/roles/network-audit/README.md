# 管理画面の通信ログ（GCE）

GCEの `platform.yml` / `site.yml` は、Cilium環境でアプリ配備前にこのロールを実行する。
`forge_app_name` からHubble収集対象（`<app>-codex/`）、ログファイル、閲覧ServiceAccountを決める。
Namespaceは既存バックエンド互換の `koyorina-observability`。単一アプリのクラスタを対象とする。

readerはNetworkPolicyで既定拒否し、CiliumNetworkPolicyで `kube-apiserver`、
同一ノードの `host`、別ノードの `remote-node` からのTCP 8081だけを許可する。
K3sのPod proxyがremote-nodeとして到着する実測に対応する。
これはAPIサーバープロセスだけの許可ではなく、他ノードのホスト上のプロセスも
readerへ接続できる範囲になる。通常のPodや外部全体を許可するものではない。
Ciliumの既定ではノードIPに
`ipBlock` が一致しないため、serverのIPをCIDRで指定する方式は使わない。

外部転送の `forge_audit_log_exporter: disabled` のまま利用できる。
既存の外部転送用 `audit-collector` とは別のDaemonSetで、画面用readerだけを配備する。
ログは100MiB × 現行1本・バックアップ5本にローテートし、readerは最新ファイルの
末尾8MiB・最大800行を読む。画面の時間指定は保持期間を保証しない。

初回とHubble設定変更時はCiliumをローリング再起動する。版は変えず、既存Helm値を維持する。
API接続先は `forge_server_node` のKubernetes InternalIPを取得して明示する。
サーバーのkubeconfigにある `127.0.0.1` はagentノードで使えないため、
Cilium CLIの自動推定を使わずHelmで更新する。接続先だけの差分も修復対象になる。
再実行時はHelmの実設定を比較し、差分がなければCiliumを再起動しない。
公式仕様: https://docs.cilium.io/en/stable/observability/hubble/configuration/export/

Ansible実行元には `setup/` に加え `contrib/observability/collector.yaml` が必要。
そこから既存のCGI処理を再利用する。Secretや外部ログ送信先の設定は不要。

```bash
cd setup/ansible
ansible-playbook -i inventory/gce.yml platform.yml
kubectl -n koyorina-observability get pods -l app=audit-collector
kubectl auth can-i list pods -n koyorina-observability --as=system:serviceaccount:ai-terakoya:ai-terakoya-viewer
```

配備後に生成Podから通信を発生させ、管理画面の通信ログを更新する。
過去の未収集通信は復元できない。HTTP本文の収集やTLS復号、DNSのL7ポリシーは追加しない。
現在のポリシーでDNS情報が得られない場合は、接続先がIPアドレス中心になる。
