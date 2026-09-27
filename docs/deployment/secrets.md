# 秘密情報の扱い

APIキー、サービスアカウント鍵、DB接続URL、セッション鍵などの認証情報は
リポジトリへコミットしない。ドメイン、レジストリ、GCPプロジェクトIDは秘密では
ないが、環境固有の値なので公開用の設定ファイルには実値を残さない。

## 設定値を置く場所

| 値 | 設定・保存先 |
|---|---|
| ドメイン、レジストリ | オンプレAnsibleは`setup/ansible/group_vars/onprem/local.yml`、GCE Ansibleは`group_vars/gce/local.yml`。手動配備は`setup/environments/dev.env`または`prod.env` |
| GCPプロジェクトID | GCEは`terraform.tfvars`と`group_vars/gce/local.yml`。オンプレでVertex AIを使う場合は配備後にGUIで設定する |
| Google OAuthクライアントID、初期管理者メール | オンプレは`ansible-playbook -e`または`group_vars/onprem/local.yml`。GCEは初回の`bootstrap-gce.py prepare`で対話入力する |
| 生成AIのAPIキー | 配備後に「マスター管理 → システム設定 → 生成AIの設定」で登録する |
| Vertex AI認証 | GUIからWorkload Identity連携を設定する。長期のサービスアカウント鍵は不要 |
| `terraform.tfvars` | `terraform.tfvars.example`をコピーして作る。Git管理対象外 |
| `terraform-outputs.json` | Terraformの出力から生成する。内部IPなどを含むためGit管理対象外 |

`local.yml`、`*.env`、`terraform.tfvars`、`terraform-outputs.json`は
[`.gitignore`](../../.gitignore)で除外している。公開前には`git status
--ignored`も確認し、フォルダ全体をZIPで公開しない。

## 生成AIのAPIキー

Gemini、Antigravity、OpenAI互換API、Claudeの設定はGUIから登録する。APIキーは
`TENANT_SECRET_KEY`で暗号化してPostgreSQLへ保存し、APIの応答や監査ログには値を
返さない。生成エージェントで必要なキーはcontrollerが専用のKubernetes Secretへ
同期し、PodへSecret参照として渡す。

`forge_gemini_api_key`と`koyorina-gemini-api` Secretは、GUI設定前からGemini
Developer APIを使う従来の環境変数経路でのみ使用する。通常の新規配備では設定せず、
GUIから登録する。

テナント別の生成AI設定も同じ暗号化鍵でDBへ保存する。Vertex AIを選んだ場合は
Workload Identity連携の識別情報だけを保存し、サービスアカウント鍵やアクセストークンは
保存しない。

## `TENANT_SECRET_KEY`

`TENANT_SECRET_KEY`を失うと、DBに保存済みのAPIキーを復号できなくなり、利用者による
再登録が必要になる。DBのバックアップと同じ期間、同じ鍵を保持する。

| 環境 | 正本と復旧方法 |
|---|---|
| GCE | Secret Managerの`koyorina-tenant-secret-key`が正本。配備時に`koyorina-tenant-secrets` Secretへ同期する |
| オンプレ | 初回配備時に`koyorina-tenant-secrets` Secretへ自動生成する。DBを残してクラスタを作り直す場合に備え、運用者が安全な場所へバックアップする |

オンプレで値をバックアップ・復元するときは、画面やログへ値を貼らず、アクセス権を
限定した秘密管理基盤を使用する。

## Kubernetes Secret

初期構築スクリプトはDB接続情報、セッション鍵、OAuth設定、controller間の認証トークンを
Kubernetes Secretとして作成する。既存値と異なる場合は不用意に上書きせず停止する。

Secretを作成・更新する処理では`kubectl create`または`kubectl replace`を使い、秘密を
含むマニフェストに`kubectl apply`を使わない。`apply`は投入内容を
`kubectl.kubernetes.io/last-applied-configuration`アノテーションへ複製するためである。
通常のDeploymentやConfigMapなど、秘密を含まないマニフェストには`apply`を使用する。

Kubernetes Secretの値は暗号化ではなくbase64エンコードで保存される。Secretの参照権限、
etcdとバックアップの保護、`kubectl get secret -o yaml`を実行できる運用者の範囲を制限する。

## Vertex AI

標準構成はGUIから設定するWorkload Identity連携で、長期鍵をクラスタへ保存しない。
GCEの管理アプリは、GUI設定を保存するまでの環境既定としてノードのサービスアカウント
（ADC）を使用できる。

`koyorina-vertex` Secretへサービスアカウント鍵を入れる経路は、既存環境の互換用である。
新規構築ではWorkload Identity連携を使用する。以前の`koyorina-vertex-token`を定期更新する
`token-file`方式は廃止している。

## 漏洩した場合

秘密をコミット、ログ出力、画面共有してしまった場合は、ファイルやGit履歴から削除する
だけでは不十分である。該当するAPIキー、サービスアカウント鍵、セッション鍵、DB資格情報を
失効またはローテーションし、利用履歴を確認する。
