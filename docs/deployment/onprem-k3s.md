# オンプレミスサーバ k3s への配備

3台程度のマシン（サーバ1台 + エージェント数台）を用意する。OSは Ubuntu 等
Debian系、または Rocky Linux 等 RHEL系（AppArmorは使えないため
`CONTROLLER_APPARMOR_PROFILE` 相当は空のままになる）。

## 1. イメージをビルド・push する

```sh
cp setup/environments/example.env setup/environments/dev.env
# dev.env のDOMAIN・REGISTRYを実際の値に書き換える

make build-push
```

## 2. Ansible で k3s とアプリを配備する

```sh
cd setup/ansible
cp inventory/onprem.example.yml inventory/onprem.yml
# inventory/onprem.yml をマシンのIP/ホスト名に合わせて編集する

cp group_vars/onprem/local.example.yml group_vars/onprem/local.yml
# local.yml のドメイン、レジストリ、IP、agent名を書き換える
```

設定は次の順で読み込まれ、後にあるファイルの値が優先される。

| ファイル | 役割 | 編集 |
|---|---|---|
| `group_vars/all/defaults.yml` | 全環境共通の既定値 | 通常は編集しない |
| `group_vars/onprem/defaults.yml` | オンプレ用の既定値と設定例 | 通常は編集しない |
| `group_vars/onprem/local.yml` | 実際のドメインやレジストリなど、この環境固有の値 | 編集する。Git管理対象外 |
| `inventory/onprem.yml` | 接続先サーバーのIP、ホスト名、SSHユーザー | 編集する。Git管理対象外 |

`group_vars/onprem/local.yml`へ設定する主な項目は次のとおり。

| パラメータ | 必要になる場合 | 設定内容 |
|---|---|---|
| `forge_domain` | 必須 | 公開するドメイン。例: `koyorina.example.com`。生成アプリは `*.koyorina.example.com` で配信するので、DNSはワイルドカードも同じIPへ向ける |
| `forge_registry` | 必須 | 手順1でイメージをpushしたレジストリ |
| `forge_metallb_ip` | Gatewayを公開する場合 | MetalLBから割り当てる未使用IP |
| `forge_agent_node` | 既定の`local-path`を使う場合 | 生成・プレビューを配置するinventory上のagent名 |
| `forge_google_oauth_client_id` | 初回構築 | Google OAuthのクライアントID |
| `forge_bootstrap_admin_email` | 初回構築 | 最初の管理者として登録するメールアドレス |
| `forge_cni` | CNIを変更する場合 | 新規構築は`cilium`、既存Flannelを使う場合は`flannel` |
| `forge_k3s_version` | k3sの版を指定する場合 | 例: `v1.37.0+k3s1` |

Geminiの接続方式、APIキー、Vertex AIのGCPプロジェクトは、配備後に
「マスター管理 → システム設定 → 生成AIの設定 → Gemini」から設定する。
`forge_gemini_api_backend`、`forge_gemini_api_key`、`forge_gcp_project`は環境変数による
初期値をあらかじめ用意したい場合だけ使い、通常の初回配備では設定しなくてよい。

ストレージは、利用環境に合う構成を1つ選ぶ。

| 構成 | 適した環境 |
|---|---|
| `local-path` | 生成・プレビューを1台のagentノードで実行する |
| NFS | 複数のagentノードから同じデータを利用する |
| 既存StorageClass | Cephなどの共有ストレージをすでに構築している |

**既定の`local-path`**

```yaml
forge_storage_mode: local-path
forge_storage_class: local-path
forge_storage_access_mode: ReadWriteOnce
forge_agent_node: k3s-agent-2
```

**NFSを複数ノードで共有**

```yaml
forge_storage_mode: nfs
forge_storage_class: koyorina-nfs
forge_storage_access_mode: ReadWriteMany
forge_nfs_server: 192.0.2.20
forge_nfs_share: forge
```

**既存のCephなどを利用**

```yaml
forge_storage_mode: external
forge_storage_class: cephfs  # 既存のStorageClass名
forge_storage_access_mode: ReadWriteMany
forge_node_selector: {}
```

OAuthクライアントIDと初期管理者メールは、`local.yml`へ保存せず、下記のように
`-e`で実行時に渡すこともできる。

```sh
# Ansibleの追加部品を準備
ansible-galaxy collection install -r requirements.yml -p ./collections

# k3sクラスタ、Cilium、ストレージなどの基盤
ansible-playbook -i inventory/onprem.yml site.yml \
  -e forge_google_oauth_client_id=xxxxx.apps.googleusercontent.com \
  -e forge_bootstrap_admin_email=admin@example.com
```

`setup/manifest/gateway.yaml` を認証・DB初期化の確認後に手で当てる
（ルートREADMEのオンプレk3s手順を参照）。

## 3. 確認する

```sh
kubectl -n koyorina get pods
kubectl -n koyorina-codex get pods

curl -sI https://<domain>/healthz
```

詳しくは [`setup/ansible/README.md`](../../setup/ansible/README.md) の「配備ロジックの境界」を参照。
既存の `setup/manifest/{provision,configure-codex,
configure-gemini-api-key}.py` と `apply.sh` を Ansible がそのまま呼ぶ構成なので、Ansibleを使わずシェルスクリプトを実行することもできる。
