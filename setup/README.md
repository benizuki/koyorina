# setup — 配備に必要なファイル

Koyorinaを動かすk3s、Kubernetesマニフェスト、GCEの基盤定義をまとめています。
実際の配備手順は、まず[ルートREADME](../README.md)を参照してください。

- [Ansibleのファイル構成と考え方](ansible/README.md)
- [秘密情報の設定](../docs/deployment/secrets.md)

## 構成

```text
setup/
├── environments/       環境ごとの設定値（dev.env / prod.env）
├── ansible/             k3sの構築とアプリ配備
├── manifest/            共通のKubernetesマニフェストと配備スクリプト
├── helm/koyorina/       GCE・アプリ配備用のHelm Chart
├── gcp/terraform/       GCE、Cloud SQL、Artifact Registryなどの基盤
├── gcp/k8s/             GCE固有の初期化・配備スクリプト
├── manifest/seccomp/    Codex用Bubblewrapのseccompプロファイル
└── preview/             生成アプリの実行環境イメージ
```

`setup/ansible`がオンプレとGCEの配備をまとめます。GCEのVMやCloud SQLを作る処理だけは
Terraformが担当します。`gcp/k8s`のPythonスクリプトは、Ansibleから呼び出される既存の
配備ロジックです。

## 環境設定のルール

ドメイン、イメージの置き場、GCPプロジェクトなど、環境で変わる値は
`setup/environments/<環境>.env`に集約します。`dev.env`と`prod.env`はコミットせず、
まず見本をコピーします。

```sh
cp setup/environments/example.env setup/environments/dev.env
cp setup/environments/example.env setup/environments/prod.env

python3 setup/environments/load.py dev
```

マニフェストの`${DOMAIN}`などは実行直前に`render.py`が置き換えます。値をコードや
マニフェストへ書き写さないでください。Ansibleを使う場合は、`group_vars`の値から
`ansible.env`が生成されます。

### よく変更する環境パラメータ

| パラメータ | 用途 |
|---|---|
| `APP_NAME` | Kubernetesリソース名の接頭辞。通常は`koyorina`のまま |
| `DOMAIN` | 公開するドメイン、OAuthのOrigin、証明書の名前 |
| `REGISTRY` | Koyorina本体のイメージ置き場 |
| `METALLB_IP` | オンプレのLoadBalancerに割り当てるIP。GCEでは空 |
| `GCP_PROJECT` | Vertex AIやGCP配備で使う値 |
| `GEMINI_API_BACKEND` | `vertex`（Vertex AI）または`developer`（AI Studio） |
| `AGENT_NODE` / `NODE_SELECTOR` | 生成・プレビューを載せるノード |
| `STORAGE_CLASS` / `STORAGE_ACCESS_MODE` | `local-path`（RWO）やNFS/Ceph（RWX） |
| `PREVIEW_*` | プレビューPodのCPU・メモリ要求と上限 |
| `IMAGE_ROOT` / `SERVER_NODE` | GCE配備スクリプトが使うイメージ置き場とサーバーノード |

生成アプリの置き場は`APP_REGISTRY_KIND`と`APP_REGISTRY_HOST`で選びます。オンプレの
クラスタ内Registryは`private`、GCEのArtifact Registryは`artifact`です。

APIキーやサービスアカウント鍵は`.env`に書かず、[秘密情報の手順](../docs/deployment/secrets.md)
に従ってSecretへ登録します。

再リリースでは既存DBを引き継がず、`backend/migrations/versions/0001_initial.py`が
現在のスキーマをまとめて作ります。既存DBを残す場合は、このスカッシュ済み履歴を
そのまま適用せず、別途データ移行計画を立ててください。

## よく使うコマンド

値の展開結果を確認するだけなら、クラスタへは接続しません。

```sh
python3 setup/environments/load.py dev
python3 setup/manifest/render.py app.yaml --environment dev
```

オンプレのk3sを更新する場合は、イメージを作ってdigestを固定してからAnsibleを実行します。

```sh
make build-push
(cd setup/ansible && ansible-playbook -i inventory/onprem.yml site.yml)
```

GCEの配備は、Terraformで基盤を作ったあと、`cluster.yml`でk3sを構築し、
`platform.yml`でアプリを配備します。コマンドの全体は[GCEの手順](../docs/deployment/gce.md)にあります。

```sh
(cd setup/ansible && ansible-playbook -i inventory/gce.yml cluster.yml)
(cd setup/ansible && ansible-playbook -i inventory/gce.yml platform.yml)
```

## ストレージ

`forge_storage_mode`（Ansible）または対応する環境値で方式を選びます。

| 方式 | 用途 | 特徴 |
|---|---|---|
| `local-path` | 追加のストレージがない場合 | ノード固定のRWO。既定値 |
| `nfs` | FilestoreやNFSをRWXで使う場合 | `csi-driver-nfs`を導入 |
| `external` | Cephなどを別途用意済みの場合 | StorageClassの存在だけ確認 |

GCEでFilestoreを使うときはTerraformの`enable_filestore = true`と
AnsibleのNFS設定を揃えます。
ストレージ方式は、初回構築前に決めてください。

## ネットワークとセキュリティ

Ansibleで新しくk3sを作ると、既定ではk3s内蔵のFlannelを最初から無効にして、
CiliumとHubbleを導入します。
既存クラスタをそのまま使う場合は`forge_cni: flannel`を指定してください。
ネットワークポリシーやHubble監査の参考実装は
[`contrib/observability`](../contrib/observability/README.md)にあります。

生成ワーカーのCodex Bubblewrapには専用のseccompプロファイルが必要です。内容と検証方法は
[manifest/seccomp/README.md](manifest/seccomp/README.md)を参照してください。

## GCE Terraformの主なパラメータ

`setup/gcp/terraform/terraform.tfvars.example`をコピーして設定します。実際の値は
`terraform.tfvars`にだけ書き、コミットしません。

stateを保存するGCSバケット名は`backend.hcl`へ設定し、次のコマンドで初期化します。

```sh
cd setup/gcp/terraform

cp backend.hcl.example backend.hcl

# backend.hclのbucketを実際に作成したGCSバケット名へ変更
terraform init -backend-config=backend.hcl
```

| パラメータ | 意味 |
|---|---|
| `project_id` | GCPプロジェクト（必須） |
| `region` / `zone` | リソースを置く場所 |
| `domain` | 公開ドメイン。`environments`の`DOMAIN`と一致させる |
| `server_machine_type` | k3sサーバと管理アプリのVMサイズ |
| `agent_machine_type` / `agent_count` | 生成・プレビュー用VMのサイズと台数 |
| `workspace_disk_gb` | 生成物とプレビューの作業領域 |
| `enable_filestore` | Filestoreを作りRWXにするか |
| `database_tier` / `database_high_availability` | Cloud SQLのサイズと冗長化 |
| `allowed_source_ranges` | LBへ接続できる送信元の制限 |
| `cloud_armor_waf_preview` | WAFを記録だけにする期間の設定 |

すべてのTerraform変数は[variables.tf](gcp/terraform/variables.tf)で確認できます。
費用や公開範囲を`terraform plan`で確認してから`apply`してください。
