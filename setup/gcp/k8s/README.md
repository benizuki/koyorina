# GCP側のクラスタへ当てるマニフェストと配備スクリプト

共通のマニフェスト（`setup/manifest/`）との差分だけをここに置く。手順は
[`docs/deployment/gce.md`](../../../docs/deployment/gce.md) を参照
（`setup/ansible` の `roles/gce-deploy` が、ここにある `.py` をサーバVM上へ
配って呼び出す）。

| ファイル | 役割 |
|---|---|
| `app-config.yaml` | 共通ConfigMapの差分。生成アプリのイメージ置き場をArtifact Registryにする |
| `bootstrap-gce.py` | 初回のみ。Cloud SQLの初期化と初期管理者登録（`prepare`/`bootstrap`） |
| `deploy-app-gce.py` | 管理アプリの配備 |
| `deploy-generation-gce.py` | 生成controller・workerの配備 |
| `deploy-preview-gce.py` | プレビューcontrollerの配備 |
| `test_bootstrap_gce.py` / `test_deploy_app_gce.py` | オフライン検証（`pytest`） |

クラスタ内Registry（`setup/manifest/registry.yaml`）はGCP側では使わない。
Artifact Registryなら同じGCPプロジェクトに収まり、脆弱性検査も付く。

**ワークロード本体（Deployment/Service/RBAC/NetworkPolicy等）と公開用のTraefik Ingressは
[`setup/helm/koyorina`](../../helm/koyorina) のHelm Chartが持つ。**
`app-base.json`・`generation-base.json`・`preview-base.json`（生マニフェストの
JSON）は廃止した。`deploy-app-gce.py`が最初に
`helm upgrade --install koyorina setup/helm/koyorina -n koyorina --create-namespace
-f <values.yaml>` を実行し、`deploy-generation-gce.py`・`deploy-preview-gce.py`は
それぞれの後段で同じコマンドを冪等に呼び直す（namespaceの所有権はChart側に
あるため、この3スクリプトではnamespaceを作らない）。
Chartが作らない・作ってはいけないもの——Secretのガード付き作成
（`ensure_pair`/`upsert_secret`）、Artifact RegistryのPull tokenやVertexトークンの
定期更新（systemdタイマー）、Cloud SQL初期化の前提確認、local-pathの
ノード紐付け——は引き続きこれらのPythonスクリプトが担う。

`values.yaml`は`--values`（既定 `/opt/koyorina-setup/helm/koyorina-values.gce.yaml`）
で指定する。当面は手作業で用意する（group_varsからの自動生成は未実装）。
イメージは`imageTag`（例: `v0.1.0-dev`）で指定でき、`helm_upgrade`が配備のたびに
`gcloud artifacts docker images describe`で3イメージのdigestへ解決して
`--set images.*.digest=...`として渡す。controllerはdigest固定のイメージしか
起動しないため、クラスタへはタグを渡さない。`imageTag`が空なら
`images.*.digest`をそのまま使う。

各スクリプトは `--environment <名前>` で `setup/environments/<名前>.env`
（`setup/ansible` を使う場合は生成される `ansible.env`）から既定値を取る。
値の意味は [`docs/deployment/secrets.md`](../../../docs/deployment/secrets.md)
と各スクリプトの `--help` を参照。
