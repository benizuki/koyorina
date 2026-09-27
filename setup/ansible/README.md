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
├── platform.yml                 アプリの配備
└── roles/                       実際の作業部品
    ├── common/                    OSパッケージ
    ├── node-profiles/             seccomp・AppArmor
    ├── k3s-server/                k3sサーバー
    ├── k3s-agent/                 k3sエージェント
    ├── cilium/                    Cilium・Hubble
    ├── storage/                   作業領域・StorageClass
    ├── manifests/                 オンプレの配備
    └── gce-deploy/                GCEの配備
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

## Playbookの流れ

```text
site.yml
├── cluster.yml
│   ├── common
│   ├── node-profiles
│   ├── k3s-server
│   ├── k3s-agent
│   ├── cilium（forge_cni=cilium の場合）
│   └── storage
└── platform.yml
    ├── manifests（オンプレの場合、localhostで実行）
    └── gce-deploy（GCEの場合、k3sサーバーで実行）
```

`site.yml`は全体を実行する入口です。クラスタだけを構築するときは`cluster.yml`、
アプリだけを再配備するときは`platform.yml`を使います。

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

## 主なAnsibleパラメータ

| パラメータ | 役割 | 主な値 |
|---|---|---|
| `forge_target` | 配備先の分岐 | `onprem` / `gce` |
| `forge_environment` | 生成する環境設定の名前 | `dev` / `prod` / `ansible` |
| `forge_domain` | 公開ドメイン | 例: `koyorina.example.com` |
| `forge_registry` | Koyorina本体のイメージ置き場 | レジストリのホストとパス |
| `forge_gcp_project` | GCPのプロジェクト | GCE・Vertex AIで使用 |
| `forge_image_root` | GCE配備スクリプトが参照するイメージのルート | Artifact Registryのパス |
| `forge_server_node` | GCEのk3sサーバーノード名 | Terraformの`server_name` |
| `forge_agent_node` | local-pathを使うときの生成ノード | エージェントのホスト名 |
| `forge_node_selector` | 生成・プレビューPodのノード選択 | Kubernetesラベルの辞書 |
| `forge_agent_toleration` | agentノードのtaintを許可 | `true` / `false` |
| `forge_google_oauth_client_id` | Google OAuthクライアントID | 秘密寄り。初回のみ |
| `forge_bootstrap_admin_email` | 初期管理者のメール | 秘密寄り。初回のみ |
| `forge_gemini_api_key` | Gemini Developer APIキー | 指定時だけSecretを更新 |
| `forge_gemini_api_backend` | Geminiの接続先 | `vertex` / `developer` |
| `forge_k3s_version` | 導入するk3sの版 | 例: `v1.37.0+k3s1` |
| `forge_allow_k3s_upgrade` | k3sの再インストール・更新を許可 | 通常は`false` |
| `forge_cni` | クラスタ内ネットワーク | `cilium` / `flannel` |
| `forge_storage_mode` | ストレージの経路 | `local-path` / `nfs` / `external` |
| `forge_storage_class` | 使用するStorageClass | 例: `local-path`, `nfs-csi` |
| `forge_storage_access_mode` | PVCのアクセス方式 | `ReadWriteOnce` / `ReadWriteMany` |
| `forge_nfs_server` / `forge_nfs_share` | NFS・Filestoreの接続先 | `nfs`の場合に指定 |

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
