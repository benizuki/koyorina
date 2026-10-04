# Ansibleの構成と設定

このディレクトリは、k3sの構築と、その上へのアプリ配備に使うAnsible定義です。
実際の実行手順は、[ルートREADME](../../README.md)のデプロイ章と、
オンプレは[`onprem-k3s.md`](../../docs/deployment/onprem-k3s.md)、GCEは
[`gce.md`](../../docs/deployment/gce.md)を参照してください。

AnsibleのコマンドはローカルPCで実行します。対象ノードへSSHし、クラスタの構築や
ファイル転送を行います。オンプレのアプリ配備だけは、ローカルPCからクラスタへ
接続して既存の`setup/manifest/`スクリプトを呼びます。GCEでは`setup/`一式を
サーバVMへ転送し、VM上で`setup/gcp/k8s/`のスクリプトを呼びます。

## ファイル構成

```text
setup/ansible/
├── ansible.cfg                  Ansibleの既定設定
├── requirements.yml             追加Collection（ansible.posix）
├── inventory/                   接続先とホストグループ
│   ├── onprem.example.yml         オンプレ用の見本
│   └── gce.example.yml            GCE用の見本（IAP経由SSH）
├── group_vars/                  グループごとの設定値
│   ├── all/defaults.yml           共通設定
│   ├── onprem/defaults.yml        オンプレ用の既定値
│   └── gce/defaults.yml           GCE用の既定値
├── site.yml                     cluster.ymlとplatform.ymlの入口
├── cluster.yml                  k3s・ネットワーク・ストレージの構築
├── storage-csi.yml              共通Ceph RBD CSIだけを追加・再適用
├── platform.yml                 アプリの配備
└── roles/                       実際の作業部品
    ├── common/                    OSパッケージ
    ├── node-profiles/             seccomp・AppArmor
    ├── k3s-server/                k3sサーバー
    ├── k3s-agent/                 k3sエージェント
    ├── cilium/                    Cilium・Hubble
    ├── storage/                   作業領域・StorageClass
    ├── ceph-rbd-csi/              k3sで共有するRBD CSI・StorageClass
    ├── manifests/                 オンプレの配備
    ├── gce-deploy/                GCEの配備
    ├── publication-node/          公開基盤のノード設定・Registry Pull設定
    ├── publication-storage/       公開データの確認・GCE PD CSI
    └── publication/               ビルド・公開基盤と内部Registryの追加配備
```

Inventoryのホストは、次のグループで分けます。

| グループ | 役割 |
|---|---|
| `onprem` / `gce` | 配備先の種類。`forge_target`の判定にも使う |
| `k3s_servers` | k3sの管理ノード。通常1台 |
| `k3s_agents` | 生成・プレビューなどを実行するノード |

たとえば、次の指定は`k3s-server-1`へ`192.168.1.10`でSSHする意味です。

```yaml
k3s_servers:
  hosts:
    k3s-server-1:
      ansible_host: 192.168.1.10
```

オンプレのInventoryでは`onprem.vars.ansible_python_interpreter`に対象ノードの
Pythonの絶対パスを指定します。見本は`/usr/bin/python3.12`です。異なるOSや
Pythonの版を使う場合は、各ノードの実際のパスに合わせて変更してください。

## Playbookの流れ

```text
site.yml
├── cluster.yml
│   ├── common
│   ├── node-profiles
│   ├── k3s-server
│   ├── k3s-agent
│   ├── cilium（forge_cni=cilium の場合）
│   ├── storage
│   ├── storage-csi.yml → ceph-rbd-csi（オンプレで有効な場合）
│   └── publication-node（公開基盤を有効にした場合）
└── platform.yml
    ├── manifests（オンプレの場合、localhostで実行）
    ├── publication-storage → publication（オンプレの公開基盤を有効にした場合）
    └── gce-deploy（GCEの場合、公開基盤の追加配備も実行）
```

`site.yml`は全体を実行する入口です。クラスタだけを構築するときは`cluster.yml`、
アプリだけを再配備するときは`platform.yml`を使います。既存クラスタへRBD CSIだけを
適用する場合は`storage-csi.yml`を実行し、その後に`platform.yml`を実行します。

## 設定値の読み込み

設定の共通部分は[`group_vars/all/defaults.yml`](group_vars/all/defaults.yml)にあります。
対象に応じて`onprem/defaults.yml`または`gce/defaults.yml`が上書きされ、
プロジェクト固有の値や秘密寄りの値はGit管理対象外の`local.yml`へ置きます。

```text
all/defaults.yml
  → onprem/defaults.yml または gce/defaults.yml
  → onprem/local.yml または gce/local.yml
  → -e で指定した値
```

`local.yml`は次のように作成します。

```sh
# オンプレ
cp group_vars/onprem/local.example.yml group_vars/onprem/local.yml

# GCE
cp group_vars/gce/local.example.yml group_vars/gce/local.yml
```

コピー後、実環境で変更する値だけを`local.yml`へ残します。

`local.yml`にはGCPプロジェクト、ドメイン、レジストリなどを置けます。
秘密情報はGitへコミットしません。

## Ansibleパラメータ

以下は`local.yml`に指定するAnsible変数です。シェルの環境変数ではありません。
既定値は共通設定を示し、GCEなどの環境別設定で上書きされる項目があります。

### 配備先・本体の公開

| パラメータ | 説明 | 共通既定値 |
|---|---|---|
| `forge_target` | 配備先の分岐 | `onprem` |
| `forge_app_name` | アプリ名・Kubernetesリソースの接頭辞 | `koyorina` |
| `forge_environment` | 生成する環境設定の名前 | `dev` |
| `forge_domain` | 公開ドメイン | `koyorina.example.com` |
| `forge_registry` | Koyorina本体のイメージ置き場 | `registry.example.com` |
| `forge_metallb_ip` | 本体NGINX Gatewayの専用IP。Registry用IPとは別 | 空欄 |

### GCP・GCE

| パラメータ | 説明 | 共通既定値 |
|---|---|---|
| `forge_gcp_project` | GCPのプロジェクト | `your-gcp-project-id` |
| `forge_image_root` | GCE配備スクリプトが参照するイメージのルート | 空欄 |
| `forge_server_node` | GCEのk3sサーバーノード名 | 空欄 |
| `forge_enable_filestore` | GCEでFilestoreを使用する場合に有効化。Terraformの設定と揃える | `false` |
| `forge_helm_version` | GCEに導入するHelmの版 | `v4.2.4` |

### 認証・生成AI

| パラメータ | 説明 | 共通既定値 |
|---|---|---|
| `forge_google_oauth_client_id` | Google OAuthクライアントID | 空欄 |
| `forge_bootstrap_admin_email` | 初期管理者のメール | 空欄 |
| `forge_gemini_api_key` | Gemini Developer APIキー | 空欄 |
| `forge_gemini_api_backend` | Geminiの接続先 | `developer` |
| `forge_vertex_api_host` | Vertex AIの接続ホスト。専用接続先がある場合のみ変更 | `aiplatform.googleapis.com` |
| `forge_codex_enabled` | 生成コントローラーの有効化 | `true` |

### k3s・クラスタネットワーク

| パラメータ | 説明 | 共通既定値 |
|---|---|---|
| `forge_k3s_version` | 導入するk3sの版 | `v1.37.0+k3s1` |
| `forge_allow_k3s_upgrade` | k3sの再インストール・更新を許可 | `false` |
| `forge_tls_san_extra` | k3s API証明書へ追加するSANの一覧 | `['{{ forge_domain }}']` |
| `forge_cni` | クラスタ内ネットワーク | `cilium` |
| `forge_cilium_version` | Ciliumの版 | `1.20.2` |
| `forge_cilium_cli_version` | Cilium CLIの版 | `v0.19.7` |
| `forge_kubectl` | ノード上で実行するkubectlコマンド | `k3s kubectl` |

### 生成・プレビューの配置と保存領域

| パラメータ | 説明 | 共通既定値 |
|---|---|---|
| `forge_agent_node` | local-pathを使うときの生成ノード | `k3s-agent-2` |
| `forge_node_selector` | 生成・プレビューPodのノード選択 | `{}` |
| `forge_agent_toleration` | agentノードのtaintを許可 | `false` |
| `forge_storage_mode` | ストレージの経路 | `local-path` |
| `forge_storage_class` | 使用するStorageClass | `local-path` |
| `forge_storage_access_mode` | PVCのアクセス方式 | `ReadWriteOnce` |
| `forge_nfs_server` | NFS・FilestoreのサーバーIP。nfsモードの場合に指定 | 空欄 |
| `forge_nfs_share` | NFSの共有名 | `forge` |
| `forge_csi_driver_nfs_version` | NFS CSIドライバーの版 | `v4.9.0` |
| `forge_generation_image_pull_secret` | 生成PodのイメージPull用Secret名 | 空欄 |
| `forge_preview_image_pull_secret` | プレビューPodのイメージPull用Secret名 | 空欄 |

### プレビューのリソース・依存パッケージ

| パラメータ | 説明 | 共通既定値 |
|---|---|---|
| `forge_preview_cpu_request` | プレビューPodのCPU要求量 | `100m` |
| `forge_preview_cpu_limit` | プレビューPodのCPU上限 | `2` |
| `forge_preview_memory_request` | プレビューPodのメモリ要求量 | `2Gi` |
| `forge_preview_memory_limit` | プレビューPodのメモリ上限 | `3Gi` |
| `forge_npm_registry` | 生成アプリのnpm取得先。空欄なら標準設定 | 空欄 |
| `forge_pypi_index` | 生成アプリのPythonパッケージ取得先。空欄なら標準設定 | 空欄 |

### ネットワーク監査

| パラメータ | 説明 | 共通既定値 |
|---|---|---|
| `forge_network_policy_mode` | 生成Pod向けポリシーの方式。CNIの選択とは独立 | `legacy` |
| `forge_audit_log_request_content` | 監査ログへのリクエスト内容記録を有効化 | `false` |
| `forge_audit_ca_configmap` | 監査通信で使うCAのConfigMap名 | 空欄 |
| `forge_audit_log_exporter` | 監査ログの出力先 | `disabled` |
| `forge_audit_splunk_index` | Splunkの監査ログ用インデックス | `koyorina_audit` |
| `forge_audit_gcp_log_name` | GCP監査ログの名前 | `koyorina-network-audit` |

### 生成アプリの公開・ビルド

| パラメータ | 説明 | 共通既定値 |
|---|---|---|
| `forge_publication_enabled` | 生成アプリのビルド・Push・公開基盤を追加配備する | `false` |
| `forge_publication_registry_host` | 内部RegistryをAnsibleで構築する場合のノードPull用ホスト名:ポート。ARだけなら不要。保存先とWIFは画面で設定 | 空欄 |
| `forge_publication_buildkit_image` | BuildKitイメージ。タグは配備時にdigestへ解決。digest固定も可能 | `moby/buildkit:v0.25.2-rootless` |
| `forge_publication_helper_image` | ビルド補助Pythonイメージ。タグは配備時にdigestへ解決 | `python:3.14-slim` |
| `forge_publication_max_builds` | ビルドの同時実行上限 | `1` |
| `forge_publication_build_timeout` | ビルド・Pushの時間上限（秒）。既定20分 | `1200` |
| `forge_publication_data_size` | 公開アプリの新規PVC容量。GCE PDは10Gi以上 | `5Gi` |
| `forge_publication_data_backend` | 公開データCSI。既存環境は`local-path`、オンプレは`ceph-rbd`、GCEは`gce-pd` | `local-path` |
| `forge_publication_data_storage_class` | 新規公開データPVCだけに使うStorageClass。既存PVCとビルドキャッシュは変更しない | `local-path` |
| `forge_publication_data_access_mode` | CSI使用時は`ReadWriteOncePod`。local-pathは`ReadWriteOnce` | `ReadWriteOnce` |
| `forge_publication_cache_size` | テナント単位のビルドキャッシュPVC容量 | `10Gi` |
| `forge_publication_node` | 公開アプリの配置ノード。空欄ならノード名による固定指定なし | 空欄 |
| `forge_publication_kubernetes_api_cidr` | コントローラーからAPIへ通信を許可するCIDR。空欄ならkubeconfigから自動取得 | 空欄 |

### 共有Ceph RBD CSI（オンプレ）

外部CephクラスタとRBDプールを先に用意します。RBD CSIとStorageClassはk3sの共通基盤として配備し、
公開アプリ以外も同じStorageClassを指定できます。CephFSのStorageClassも従来どおり利用できます。
Namespace・Helmリリース・CephX Secretは`ceph-rbd-csi`という名前で配備します。
`local.yml`には次を設定し、CephXのキーはAnsible Vaultに置きます。
キーにはCephクラスタ側で`ceph auth get-key client.<ユーザーID>`を実行して得た値を使います。
Monitorの一覧は既存のCephFS CSI設定と照合してください。CephXキーの長さは暗号方式で異なり、
新しいAES256K形式は約60文字です。`ceph auth get-key`の結果を長さで切り詰めないでください。
Monitorの形式が誤っている場合やRBD接続・マウントに失敗した場合は、実PVC検証で配備を止めます。

| パラメータ | 説明 | 既定値 |
|---|---|---|
| `forge_ceph_rbd_enabled` | 共通RBD CSIを配備する | 公開データが`ceph-rbd`ならtrue |
| `forge_ceph_rbd_csi_version` | Ceph RBD CSI Helm chartの固定版。AES256Kキーに対応 | `3.16.3` |
| `forge_ceph_rbd_storage_class` | 共通RBD StorageClass名 | 公開データ用の設定値を継承 |
| `forge_ceph_rbd_cluster_id` | CephクラスタのFSID | 空欄 |
| `forge_ceph_rbd_monitors` | Ceph Monitorの`IP:port`一覧 | `[]` |
| `forge_ceph_rbd_pool` | 既存RBDプール名 | 空欄 |
| `forge_ceph_rbd_user_id` | RBDプールへ作成・削除できるCephXユーザーID | 空欄 |
| `forge_ceph_rbd_user_key` | CephXキー。Vaultから参照 | 空欄 |
| `forge_ceph_rbd_topology_label` / `forge_ceph_rbd_topology_value` | 全ノードに付ける共有RBDドメイン | `storage.koyorina.io/rbd-domain` / `shared` |

従来の`forge_publication_rbd_*`値は互換性のため継承します。新規設定では`forge_ceph_rbd_*`を使います。
`WaitForFirstConsumer`に必要なトポロジーキーをRBD CSIが広告するよう、Ansibleが全ノードへ
同じRBDドメインラベルを付けます。ノードが増えた場合も`storage-csi.yml`を再実行します。
CSI・StorageClassの配備後、RWOPの実PVCを作成してマウント・読み書きまで検査します。
Secret参照先などStorageClassの変更不能な項目が変わった場合は、同Classを使うPV・PVCが
ないことを確認してからAnsibleがStorageClassを作り直します。使用中なら削除せず停止します。

```sh
cd setup/ansible
ansible-playbook -i inventory/onprem.yml storage-csi.yml --ask-vault-pass
ansible-playbook -i inventory/onprem.yml platform.yml --ask-vault-pass
```

公開アプリで使う場合は、次の値を指定します。
- `forge_publication_data_backend: ceph-rbd`
- `forge_publication_data_storage_class: ceph-rbd`
- `forge_publication_data_access_mode: ReadWriteOncePod`

を指定します。
既存の`local-path` PVCは変更されません。

### GCE公開データ（Compute Engine PD CSI）

GCEの自己管理k3sへPD CSIを手動で入れる構成は、GoogleのGKE向け公式サポート対象外です。
VMを同一ゾーンに置き、`pd-balanced`の10Gi以上を指定します。Regional PDと別ゾーン復旧は対象外です。
公式PD CSI v1.20.0のマニフェストを固定して同梱し、CSIコントローラーは鍵を使わずWIFで認証します。

初回構築では次の順に実行します。

1. Terraformを通常どおり適用し、`cd setup/ansible && ansible-playbook -i inventory/gce.yml cluster.yml`でk3sを起動します。
2. `kubectl`がクラスタへ接続できる状態で、`python3 setup/gcp/terraform/export-pd-csi-oidc.py /secure/path/pd-csi-jwks.json`を実行します。表示された2値をTerraformの`terraform.tfvars`に追加します。JWKSは公開鍵ですが、鍵の更新に合わせて再取得・再適用します。
3. `cd setup/gcp/terraform && terraform apply`を再実行します。出力の`publication_pd_csi_wif_audience`と`publication_pd_csi_service_account`を`group_vars/gce/local.yml`の同名接頭辞`forge_`付きの設定へ転記します。
4. `forge_publication_enabled: true`、`forge_publication_data_backend: gce-pd`、`forge_publication_data_storage_class: koyorina-published-pd`、`forge_publication_data_access_mode: ReadWriteOncePod`、`forge_publication_data_size: 10Gi`を設定し、`cd setup/ansible && ansible-playbook -i inventory/gce.yml site.yml`を実行します。

| パラメータ | 説明 | 既定値 |
|---|---|---|
| `forge_publication_pd_csi_version` | 固定版PD CSIドライバー | `v1.20.0` |
| `forge_publication_pd_csi_wif_audience` | Terraform出力のWIFプロバイダーaudience | 空欄 |
| `forge_publication_pd_csi_service_account` | Terraform出力のCSI専用GCPサービスアカウント | 空欄 |

AnsibleはCSI Pod・StorageClass・RWOPの実PVCマウントを検査します。失敗時は公開基盤の切り替えを止めます。
配備後の別ノード引き継ぎ試験は、実クラスタのkubeconfigを設定して次を実行します。
テスト用PVCとPodはスクリプトが作成・削除します。GCEでは同一ゾーンのReadyノードを選びます。

```sh
python3 setup/storage/verify_storage.py \
  --storage-class koyorina-published-pd --driver pd.csi.storage.gke.io \
  --size 10Gi --relocate
```

オンプレではStorageClassを`ceph-rbd`、ドライバーを`rbd.csi.ceph.com`に変更します。
既存の`local-path` PVCは自動移行されません。停止した公開アプリを「公開アプリ運用」で削除すると
登録・利用許可・環境変数・Service・PVCが削除され、ビルド履歴とイメージは残ります。
その後、ビルド済み版を再デプロイすると新しいCSI上に空のデータPVCが作られます。
削除はデータを失うため、必要なSQLiteデータは事前にバックアップしてください。

### 内部Registryの公開・保存領域

| パラメータ | 説明 | 共通既定値 |
|---|---|---|
| `forge_publication_internal_registry` | 内部Registryサーバーを構築する。ARだけ使う場合はfalse | `true` |
| `forge_publication_registry_service_type` | Registry Service種別。通常LoadBalancer、互換用途でNodePortも可 | `LoadBalancer` |
| `forge_publication_registry_load_balancer_ip` | Registry専用固定IPv4。LoadBalancer方式では必須。MetalLBプール内の未使用IP | 空欄 |
| `forge_publication_registry_load_balancer_class` | 既存MetalLBが処理するクラス名 | `metallb.io/l2` |
| `forge_publication_registry_service_port` | LoadBalancerで公開するポート。hostのポートと揃える | `30500` |
| `forge_publication_registry_node_port` | NodePort方式の公開ポート。LoadBalancer方式では固定NodePortを要求しない | `30500` |
| `forge_publication_registry_cidr` | Push先への通信許可CIDR。空欄なら専用IPから/32を生成、専用IPなしならDNSから取得。IP割り当てには使わない | 空欄 |
| `forge_publication_registry_storage_size` | 内部Registryの新規PVC容量 | `50Gi` |
| `forge_publication_registry_storage_class` | 内部RegistryのStorageClass。構築済みCephFSも指定可 | `local-path` |
| `forge_publication_registry_access_mode` | 内部Registry PVCのアクセス方式。CephFSならReadWriteMany、local-pathはReadWriteOnce | `ReadWriteOnce` |

### 内部Registryの認証・TLS

| パラメータ | 説明 | 共通既定値 |
|---|---|---|
| `forge_publication_registry_username` | Registry側の認証ユーザー名と初期Push/Pull設定 | 空欄 |
| `forge_publication_registry_password` | Registry側のパスワードと初期Push/Pull設定。Ansible Vaultで保護する | 空欄 |
| `forge_publication_registry_tls_enabled` | 内部RegistryのTLSを有効化。falseならHTTP＋パスワード認証 | `false` |
| `forge_publication_registry_ca_file` | TLS使用時のCA証明書。Ansible実行PC上の実ファイルの絶対パス | 空欄 |
| `forge_publication_registry_cert_file` | 内部Registry構築時のサーバー証明書。TLS使用時のみ | 空欄 |
| `forge_publication_registry_key_file` | 内部Registry構築時のサーバー秘密鍵。TLS使用時のみ | 空欄 |

### Artifact Registryの互換設定

| パラメータ | 説明 | 共通既定値 |
|---|---|---|
| `forge_publication_gcp_credentials_secret` | 従来方式のGCP資格情報Secret。画面から設定するWIFでは不要 | 空欄 |
| `forge_publication_gcp_audience` | 従来方式の投影トークンaudience。画面から設定するWIFでは不要 | 空欄 |
| `forge_publication_scanning_enabled` | AR脆弱性検査の表示用既定値。GCP側の実際の有効設定と揃える | `false` |

### 公開基盤の実行パス（通常は変更不要）

| パラメータ | 説明 | 共通既定値 |
|---|---|---|
| `forge_publication_operator_python` | 公開基盤の配備スクリプトを動かすPython | `{{ ansible_playbook_python }}` |
| `forge_publication_scripts_dir` | 公開基盤の配備スクリプトディレクトリ | `{{ playbook_dir }}/../publication` |
| `forge_publication_kubeconfig` | 公開基盤の配備に使うkubeconfig | `{{ playbook_dir }}/.kubeconfig-{{ forge_target }}` |
| `forge_publication_api_server_override` | API接続先CIDR取得用の上書き。GCEロールがサーバーIPを指定 | 空欄 |

内部RegistryはMetalLBで直接公開します。`forge_publication_registry_gateway_enabled`は廃止済みです。
本体のNGINX Gatewayには影響しません。配備時に既存Registry専用Gatewayを撤去し、PVCを保持します。
DNSはノードとビルドPodの両方からRegistry専用IPへ解決できるようにしてください。

ARのWIFプール・プロバイダー・Push用/Pull用サービスアカウントは、配備後に管理者の
「システム設定」から登録します。オンプレからARを使う場合も同じ手順です。
GCEは内部Registryを配備せず、生成アプリの保存先は画面で設定します。
内部Registryの画面設定はクライアントの接続・認証設定であり、Registry側のユーザー作成や
ノードのDNS設定やRegistryサーバー自体のTLS設定を置き換えません。
画面の「HTTPで接続する」は、接続テスト・公開時に各ノードのPull設定へ自動反映します。
`publication`ロールが専用の`registry-node` DaemonSetを追加配備します。書き込み先は
Ansibleで指定したRegistryの`hosts.toml`用ディレクトリのみで、Docker/containerdソケットは渡しません。
Registryの接続先はポート付きで指定し、変更する場合はAnsibleの接続先も更新してください。
接続方式だけの変更にはk3sの再起動は不要です。

具体的な設定例と再配備手順は[setup READMEの公開基盤設定](../README.md)を参照してください。

全パラメータの既定値とコメントは[`group_vars/all/defaults.yml`](group_vars/all/defaults.yml)、
環境別の項目は[`group_vars/gce/defaults.yml`](group_vars/gce/defaults.yml)と
[`group_vars/onprem/defaults.yml`](group_vars/onprem/defaults.yml)を参照してください。

## CNIとストレージの注意点

新規k3sの既定はCiliumです。`k3s-server`が最初からFlannelを無効にし、`cilium`ロールが
CiliumとHubbleを導入します。既存のFlannelクラスタを停止・排水してCiliumへ移行する
手順はありません。既存クラスタをそのまま使う場合は`forge_cni: flannel`を指定します。

ストレージの既定は`local-path`（ノード固定のRWO）です。FilestoreやNFSをRWXで使う場合は
`forge_storage_mode: nfs`、`forge_nfs_server`、`forge_storage_access_mode: ReadWriteMany`
を設定します。Cephなどを別途用意済みなら`external`を使い、既存StorageClass名を
`forge_storage_class`へ指定します。

## 配備ロジックの境界

Ansibleは作業の順番と設定値を管理し、既存の配備スクリプトを置き換えません。

- オンプレ: `setup/manifest/provision.py`、`apply.sh`、各`configure-*.py`
- GCE: `setup/gcp/k8s/bootstrap-gce.py`、`deploy-*-gce.py`

Secret作成、トークン発行、digest解決、DB初期化などの詳細は各スクリプト側にあります。
AnsibleでKubernetesリソースを直接操作するのではなく、k3sノードにある`k3s kubectl`や
これらのスクリプトを使う設計です。
