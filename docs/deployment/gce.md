# GCE への配備

## 1. Terraform でGCPリソースを作る

```sh
cd setup/gcp/terraform
cp terraform.tfvars.example terraform.tfvars
cp backend.hcl.example backend.hcl
# project_id, domain を書き換える。Filestoreを使うなら enable_filestore = true
# backend.hclのbucketを、実際に作成するGCSバケット名へ書き換える

# state保存用バケットを初回だけ作成する
TFSTATE_BUCKET=your-unique-koyorina-tfstate-bucket
GCP_PROJECT=your-gcp-project-id
GCP_REGION=asia-northeast1
gcloud storage buckets create "gs://${TFSTATE_BUCKET}" \
  --project="${GCP_PROJECT}" --location="${GCP_REGION}" --uniform-bucket-level-access
gcloud storage buckets update "gs://${TFSTATE_BUCKET}" --versioning

terraform init -backend-config=backend.hcl
terraform plan -out=plan.tfplan   # 費用と公開範囲を確認する
terraform apply plan.tfplan

terraform output -json > ../../ansible/terraform-outputs.json
```

- DNSのAレコードを `entrance_ip` 出力の値へ向ける。`<domain>` と `*.<domain>`（生成アプリ用。
  プレビュー・公開版は管理画面と別オリジンのサブドメインで配信する）の両方が要る。
- 証明書は`dns_authorization_record` 出力のCNAMEを追加してから発行される。

## 2. イメージをビルド・pushする

```sh
gcloud builds submit . --project=<project> --region=<region> --config=cloudbuild.yaml \
  --substitutions=_TAG=v0.1.0-alpha
```
- （`_TAG`を省くとビルドIDがタグになる。手順3の`imageTag`と同じ値にする）  

- （詳細は [`docs/deployment/cloud-build.md`](./cloud-build.md)。push先のリポジトリ名
`_REPOSITORY` は Terraformが作った Artifact Registry の名前
（既定は`var.name`と同じ）と揃えること）

- push先のパスをAnsible側にも伝える。

```sh
terraform -chdir=setup/gcp/terraform output -raw platform_registry

cp setup/ansible/group_vars/gce/local.example.yml \
  setup/ansible/group_vars/gce/local.yml
```

- 出力値を `setup/ansible/group_vars/gce/local.yml` の `forge_registry` に設定する。
（既定の`registry.example.com`のままだと、生成されるマニフェストが存在しないホストを指す）

## 3. Helm用のvalues.yamlを用意する

管理アプリ・生成controller・プレビューcontrollerのワークロード本体は
[`setup/helm/koyorina`](../../setup/helm/koyorina) のHelm Chartが持つ。  
（`deploy-app-gce.py`等が内部で`helm upgrade --install`を呼ぶ。
[`setup/gcp/k8s/README.md`](../../setup/gcp/k8s/README.md)参照）

```sh
cp setup/helm/koyorina-values.gce.yaml.example setup/helm/koyorina-values.gce.yaml
```

- コメントに沿って、domain・registry・`imageTag`（手順2の`_TAG`。配備時に
3イメージのdigestへ自動で解決される）・`serverNodeSelector`/`nodeSelector`（terraform出力の
`server_name`/`agent_names`）・`vertexProject`を埋める。  

 - `setup/`一式のサーバVMへの転送（`roles/gce-deploy`）に含まれて
自動で運ばれるので、配置場所・ファイル名は変えないこと。

## 4. Ansible で k3s を導入する

```sh
cd setup/ansible

cp inventory/gce.example.yml inventory/gce.yml
# terraform-outputs.json の server_name / agent_names / server_internal_ip /
# agent_internal_ips をinventory/gce.ymlへ書き写す

# Ansibleの追加部品を準備
ansible-galaxy collection install -r requirements.yml -p ./collections
```

### Filestore（RWX）を使う場合

この設定は`cluster.yml`を実行する前に行う。手順1で`terraform.tfvars`の
`enable_filestore = true`を設定して`terraform apply`したうえで、Filestoreの
IPアドレスと共有名を確認する。

```sh
terraform -chdir=../gcp/terraform output -raw filestore_ip
terraform -chdir=../gcp/terraform output -raw filestore_share
```

`group_vars/gce/local.yml`へ次の値を設定する。

| 設定項目 | 設定値 | 用途 |
|---|---|---|
| `forge_enable_filestore` | `true` | GCEでFilestoreを使うことをAnsibleへ伝える |
| `forge_storage_mode` | `nfs` | NFS用のStorageClassを構築する |
| `forge_storage_class` | `koyorina-nfs` | 生成・プレビューのPVCで使用するStorageClass |
| `forge_storage_access_mode` | `ReadWriteMany` | 複数ノードから同じ領域を利用できるようにする |
| `forge_nfs_server` | `filestore_ip`の出力値 | 接続するFilestoreを指定する |
| `forge_nfs_share` | `filestore_share`の出力値 | Filestoreの共有名を指定する |
| `forge_node_selector` | `{workload: koyorina-agent}` | 生成・プレビューPodをagentノードへ配置する |
| `forge_agent_toleration` | `true` | agentノードのtaintを許可する |

Cephなど、RWX対応のStorageClassをすでに用意している場合は
`forge_storage_mode: external`を指定する。  

- ストレージクラスの選択については、[`storage.md`](storage.md)を参照する。

各パラメータの設定が完了したら、クラスタ基盤を構築する。

```sh
# k3sクラスタ、Cilium、ストレージなどの基盤
ansible-playbook -i inventory/gce.yml cluster.yml
```


設定項目の詳細については、[`setup/ansible/README.md`](../../setup/ansible/README.md)を参照する。

## 5. 初回だけ手動: Cloud SQLの初期化とOAuth設定

`setup/gcp/k8s/bootstrap-gce.py prepare` はGoogle OAuthクライアントIDと
管理者メールを対話的に (`input()`) 受け取る、意図して自動化していない
手順（値をargv/envへ残さないための設計）。サーバVMへ入って一度だけ実行する。

```sh
gcloud compute ssh koyorina-server --zone=<zone> --project=<project>

sudo env PATH=/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin \
  python3 /opt/koyorina-setup/gcp/k8s/bootstrap-gce.py prepare
```
- `platform.yml` を一度も流していない場合は、先に `setup/gcp/k8s/` 一式を
サーバVMへ転送してから実行する。
- `gce-deploy` ロールが以後は自動で
`/opt/koyorina-setup/` に置く。
- `sudo`の`secure_path`には`/usr/local/bin`
（k3s/kubectlの置き場）が含まれないため、`env PATH=...`で明示的に補う必要がある。

### セットアップのために必要な情報:
- Google OAuthのウェブアプリ用クライアントID
- 初期管理者のメール
- 公開するHTTPS Origin（例: `https://koyorina.example.com`）

OAuthの承認済みJavaScript生成元にも同じOriginを登録しておく。  
DB接続URLはSecret Manager の `koyorina-database-url` から読み込み、セッション鍵は
自動生成する。  
sslmodeが無ければ`require`を付け、平文接続を指定していたら
停止する。既存Secretの値と食い違う場合は上書きせず停止する。

続けて移行と初期管理者登録を行う:

```sh
# 設定ジョブの投入
sudo env PATH=/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin \
  python3 /opt/koyorina-setup/gcp/k8s/bootstrap-gce.py bootstrap

# 設定ジョブの完了待ち
sudo env PATH=/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin \
  k3s kubectl -n koyorina wait --for=condition=complete \
  job/koyorina-gce-bootstrap-v1 --timeout=660s

# 設定ジョブのログ確認
sudo env PATH=/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin \
  k3s kubectl -n koyorina logs job/koyorina-gce-bootstrap-v1
```

`Cloud SQL migration and admin bootstrap: OK` が出れば成功。

## 6. Ansible でアプリを配備する

```sh
# Koyorinaを配備
ansible-playbook -i inventory/gce.yml platform.yml
```

- 管理アプリ・生成・プレビューのcontrollerが順に上がる。  
イメージ取得用の短期トークンは、サーバVM上のsystemd timerが20分ごとに更新する。  
（`deploy-{app,generation,preview}-gce.py`がそれぞれ
install_timerとして設定する。VM再作成時はAnsible再実行でtimerも作り直る）。

- Geminiを使用するためのAPIキー、もしくは、Vertex AI 認証は、配備後に画面（マスター管理 → システム設定 → 生成AIの設定 → Gemini）で  
Workload Identity 連携を設定する。  
画面の「GCP側の設定手順」に、クラスタの発行元・公開鍵（jwks.json）と gcloud のコマンドが出る。  

- 画面から登録したAPIキーは、`TENANT_SECRET_KEY` で暗号化してCloud SQLに保存される。  
この鍵の正本はSecret Managerの `koyorina-tenant-secret-key`で、
`deploy-app-gce.py` が初回に生成して入れ、k8sの `koyorina-tenant-secrets` へ写す。  
両者の値が食い違う場合、配備はどちらも書き換えずに停止する。  
この鍵を消す・入れ替えると保存済みのAPIキーは再登録が必要になる
（Terraformでは`deletion_protection = true`）。

## 7. 確認

```sh
sudo env PATH=/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin k3s kubectl -n koyorina get pods -o wide

sudo env PATH=/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin k3s kubectl -n koyorina-codex get deployment,pod,svc,pvc -o wide

sudo env PATH=/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin k3s kubectl -n koyorina-preview get deployment,pod,svc,pvc -o wide
```

## 8. 公開を確認する

Traefik IngressはHelm Chartの一部（`koyorina-values.gce.yaml`の
`ingress.enabled: true`）として、手順6の`platform.yml`で当たっている。  
hostは`domain`（runtime SecretのAPP_ORIGINのホスト名・terraformのdomainと
同じ値）から作られる。

サーバVMで:

```sh
curl -fsS -H 'Host: <domain>' http://<serverの内部IP>/healthz
```

- `{"status":"ok"}` が返ればTraefikからアプリまで通っている。  
LBのヘルスチェックが `HEALTHY` になったら公開ドメインのAレコードをLBのIPへ向け、
Certificate ManagerのDNS認証用CNAMEを登録して証明書が`ACTIVE`になるのを
待つ。  

## トラブルシューティング

**プレビュー実行中に `OOMKilled` になる、または初回ビルドが`Reached heap limit`**   
で止まるなどのトラブルは、VM・コンテナ・Node.jsの3段のメモリ上限が関係する。  

既定は agent VM が `e2-standard-2`（2 vCPU・8 GiB）、プレビュー
Podのmemory requestが2Gi・limitが3Gi、Node.jsの`--max-old-space-size`が
2048MiB。ヒープ以外の領域やPython側にもメモリが要るため、ヒープと
コンテナ上限を同じ値にしない。  

VM自体のメモリが足りない場合は
`terraform.tfvars` の `agent_machine_type` を上げてから`terraform apply`を行う。

**`bwrap: Failed to make / slave: Permission denied`。** ノードのseccomp/
AppArmorプロファイルの配置漏れ、またはRocky LinuxならSELinuxのAVC拒否を疑う
（`setup/README.md`の該当節、`dmesg | grep avc`）。
