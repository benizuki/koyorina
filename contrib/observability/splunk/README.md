# Splunkアプリの導入手順

## 1. アプリbundleを作成する

```sh
tar -C contrib/observability/splunk -czf koyorina-network-audit.tgz koyorina-network-audit
```

## 2. Splunkへ登録する

Splunk管理者として、**Apps > Manage Apps > Install app from file**を開き、作成した
`koyorina-network-audit.tgz`を登録します。

HEC tokenにはイベントを取り込む権限しかないため、DashboardやSaved Searchの登録には
Splunk管理者権限が必要です。

## 3. indexとアラートを有効にする

1. `koyorina_audit` indexを作成する。
2. `Koyorina Network Audit`アプリが有効になっていることを確認する。
3. 同梱されている4件のSaved Searchアラートを有効にする。
4. Dashboardで許可・拒否、未知ドメイン、利用者・テナント別の接続先、HTTP status、通信量、
   時系列を確認する。

このbundleにHEC endpoint、token、利用者のアドレス、prompt、HTTP header、query、bodyは
含まれていません。
