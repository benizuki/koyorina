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
| `DOMAIN` | 公開するドメイン、OAuthのOrigin、証明書の名前。生成アプリは `*.DOMAIN` で配信するので、DNSは `DOMAIN` と `*.DOMAIN` の両方を向ける |
| `REGISTRY` | Koyorina本体のイメージ置き場 |
| `METALLB_IP` | オンプレのLoadBalancerに割り当てるIP。GCEでは空 |
| `GCP_PROJECT` | Vertex AIやGCP配備で使う値 |
| `GEMINI_API_BACKEND` | `vertex`（Vertex AI）または`developer`（AI Studio） |
| `AGENT_NODE` / `NODE_SELECTOR` | 生成・プレビューを載せるノード |
| `STORAGE_CLASS` / `STORAGE_ACCESS_MODE` | `local-path`（RWO）やNFS/Ceph（RWX） |
| `PREVIEW_*` | プレビューPodのCPU・メモリ要求と上限 |
| `IMAGE_ROOT` / `SERVER_NODE` | GCE配備スクリプトが使うイメージ置き場とサーバーノード |

生成アプリの置き場は管理者が「システム設定」で選びます。内部Registryのサーバー構築を
Ansibleで有効にする場合だけ、ノードPull用のホスト名など基盤の設定が必要です。

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

### オンプレに内部Registry・公開基盤を追加する

既存環境でも同じ `site.yml` で追加できます。既定は無効です。
公開機能を含む管理アプリのイメージを先にビルド・digest固定し、
`setup/ansible/group_vars/onprem/local.yml` に以下を設定してください。

```yaml
forge_publication_enabled: true
forge_publication_internal_registry: true
forge_publication_registry_host: registry.internal.example.com:30500
forge_publication_registry_username: koyorina-builder
forge_publication_registry_password: "{{ vault_registry_password }}" # Ansible Vaultで保管
forge_publication_registry_tls_enabled: false # HTTP＋パスワード認証（既定）
```

内部Registryの到達先CIDRもホスト名から自動取得するため、通常は設定不要です。
Ansible実行PCでもRegistry名を解決できるようにしてください。
実行PCから名前解決できない場合、または複数IPを返す場合は
`forge_publication_registry_cidr: 192.168.110.70/32` のように到達先を明示します。

k3s APIの到達先CIDRは配備用kubeconfigから自動取得します。
通常は `forge_publication_kubernetes_api_cidr` の設定も不要です。
複数IPを持つAPI接続先や、Podからの到達先が異なる構成では、この値を明示してください。

BuildKit・Python helperはAnsibleが既定のイメージからdigestを自動取得するため、
通常は設定不要です。Dockerのインストールも不要です。実行PCからDocker HubへのHTTPS接続が必要です。
タグの更新時は再配備で新しいdigestへ切り替わります。オフライン配備や版を固定したい場合だけ
`forge_publication_buildkit_image`・`forge_publication_helper_image` に `repository@sha256:…` を指定してください。
以前の設定例にある `<…digest>` の行は削除してください。空文字の場合も既定のイメージを使います。

実行PCには `helm`・`kubectl`・`htpasswd` が必要です。HTTPでは証明書・CAは不要です。
TLSを使う場合は `forge_publication_registry_tls_enabled: true` とし、
`forge_publication_registry_ca_file`・`forge_publication_registry_cert_file`・
`forge_publication_registry_key_file` に実行PC上の実ファイルを指定します。
その場合は `openssl` も必要で、証明書のSANにRegistryのホスト名を含めます。
内部RegistryはMetalLBで直接公開します。専用固定IPからRegistry Serviceへ接続します。
本体のNGINX Gatewayとは別経路です。
`local.yml`で専用IPを明示してください。CIDRは通信許可用で、IP割り当てには流用しません。

```yaml
forge_publication_registry_service_type: LoadBalancer
forge_publication_registry_load_balancer_ip: 192.168.110.246
forge_publication_registry_load_balancer_class: metallb.io/l2
forge_publication_registry_service_port: 30500
forge_publication_registry_host: koyorina-registry.benizuki.local:30500
forge_publication_registry_cidr: 192.168.110.246/32
```

MetalLBのcontroller/speakerが処理するクラス（既存クラスタでは`metallb.io/l2`）を指定し、
専用IPが既存IPAddressPool・L2Advertisementの対象であることを確認してください。
以前のRegistry専用NGINX Gatewayは再配備時に撤去し、専用IPをRegistry Serviceへ移します。
クラス変更が必要な場合はServiceを再作成します（短時間の接続停止あり）。
Registry Deployment・PVCと本体のGatewayは削除しません。配備後にRegistry ServiceのEXTERNAL-IPが指定IPであることを確認します。
TLS使用時はRegistryで終端します。
DNSは全ノードとビルドPodからLoadBalancer IPへ解決できるようにし、TCP 30500を許可してください。
Ansibleはk3sの`coredns-custom`へRegistry名と専用IPのレコードを追加し、Pod側の名前解決を構成します。
既存のカスタムDNS設定は保持します。ノード側はFortiGate等のDNSへ同じ名前・IPを登録してください。
Serviceポートを変更するときはhost側のポートも揃えます。既存のNodePort方式を維持する場合は
`forge_publication_registry_service_type: NodePort`とし、DNSをノードIPへ向けます。
`forge_publication_registry_node_port`の既定は30500です。Service変更でPVCは削除しません。

HTTPでは認証情報・イメージは暗号化されません。FortiGate等でTCP 30500の接続元を基盤に限定してください。
Cilium環境では、公開コントローラーからAPIサーバーへの専用CiliumNetworkPolicyも配備します。
Ansibleが認証付きRegistry、Push用Secret、ノードのcontainerd認証設定、
公開コントローラー、ノードのPull接続方式を同期する`registry-node`、アプリ接続を配備します。
Ansibleによるcontainerd認証設定の変更時はk3sを再起動します。
既存のRegistry PVCとコントローラー認証トークンを維持し、公開データを削除しません。
保存容量の初期値はRegistry 50Gi、公開アプリ5Gi、テナント別キャッシュ10Giです。
`forge_publication_registry_storage_size`・`forge_publication_data_size`・
`forge_publication_cache_size` で新規領域の容量を、`forge_publication_max_builds`（既定1）・
`forge_publication_build_timeout`（秒、既定1200）でビルド制限を変更できます。
RegistryをCephFS＋RWXに置く場合は `local.yml` に以下を追加します。
StorageClassは事前に構築済みのものを指定してください。Registryは1レプリカのままです。

```yaml
forge_publication_registry_storage_class: cephfs # 実際のStorageClass名
forge_publication_registry_access_mode: ReadWriteMany
forge_publication_registry_storage_size: 50Gi
```

既定は `local-path`＋`ReadWriteOnce` です。この設定はRegistry専用です。
公開アプリのSQLite用領域は別設定で、オンプレはCeph RBD、GCEはPD CSIへ切り替えられます。
設定と初回構築順序は[Ansibleの説明](ansible/README.md)を参照してください。
オンプレのCeph RBD CSIはk3s共通基盤として`storage-csi.yml`で単独適用でき、
公開アプリ以外も同じRBD StorageClassを使えます。
既存Registry PVCの容量・StorageClass・アクセスモードは自動変更しません。
作成済みのPVCをCephFSへ切り替える場合は、新しいPVCへのデータ移行が必要です。

保存先は管理者の「システム設定」→「生成アプリのイメージ保存先」で切り替えます。
内部Registryの接続先（ホスト名:ポート）・ユーザー名・パスワード・HTTP使用も同画面で設定できます。
パスワードは暗号化保存し再表示しません。空欄なら既存値を保持します（接続先・ユーザー名の変更時は再入力）。
接続先を空欄にするとAnsibleの接続・認証設定を使用します。Registry側の認証設定、
ノードのDNS、RegistryサーバーのTLS、接続先CIDR・ポートの通信許可、PVCは引き続きAnsibleで管理します。
画面の「HTTPで接続する」は接続テスト・公開時に全ノードのPull設定にも反映します。
初回は新しい本体イメージを配備して`site.yml`を再実行し、`registry-node`を追加してください。
以後、接続方式の変更だけならk3s再起動は不要です。接続先の変更はAnsibleも更新します。
オンプレのk3sからもARを利用できます。両方を使う環境は上の内部Registry構築設定を維持し、
画面でARリポジトリとWIF（Push用・Pull用の別サービスアカウント）を設定してください。
公開鍵の取得とGCP側の設定コマンドも同画面に表示します。秘密鍵や固定GCPパスワードは不要です。

ARだけ使う場合は `forge_publication_internal_registry: false` にします。
AnsibleにARの接続先は不要です。初回ビルド前に画面で保存先とWIF設定を保存します。
GCEも `forge_publication_enabled: true` で公開基盤を追加配備できます。保存先は画面で選びます。
画面から内部Registryへ戻す場合は、配備設定で内部Registryを準備しておいてください。

配備後は[公開基盤の検証手順](../docs/deployment/publication.md)で
ビルド・Push・ノードからのPull・公開利用を確認してください。

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
