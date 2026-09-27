# Cloud Build でビルド・push

リポジトリルートで実行する。ローカルの Docker / make は不要。

```powershell
gcloud builds submit . --project=your-gcp-project-id --region=asia-northeast1 --config=cloudbuild.yaml
```

Cloud Build API を有効にし、実行サービスアカウント（`--service-account`を
指定しない場合は既定のCompute Engineサービスアカウント、
`<プロジェクト番号>-compute@developer.gserviceaccount.com`）に次の3つが必要。  

新規プロジェクトでは既定で1つも付いていないため、初回は必ず明示的に付与すること。

```sh
PROJECT_NUMBER="$(gcloud projects describe your-gcp-project-id --format='value(projectNumber)')"
SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"

for role in roles/storage.objectViewer roles/logging.logWriter roles/artifactregistry.writer; do
  gcloud projects add-iam-policy-binding your-gcp-project-id \
    --member="serviceAccount:${SA}" --role="$role"
done
```

- `roles/storage.objectViewer` — 送信したソースの一時アーカイブ（Cloud Storage）を読む。
  無いと `could not resolve source` で失敗する。
- `roles/logging.logWriter` — ビルドログをCloud Loggingへ書く。無いとビルド開始直後に失敗する。
- `roles/artifactregistry.writer` — イメージのpush。無いと3イメージのビルド自体は
  成功するが、最後のpushだけ `denied: Permission 'artifactregistry.repositories.uploadArtifacts'`
  で失敗する（ビルドステップの成功だけを見ていると気づきにくい）。

## 出力

push 先は `${_REGION}-docker.pkg.dev/${PROJECT_ID}/${_REPOSITORY}/${_APP_NAME}`
（例: `asia-northeast1-docker.pkg.dev/your-gcp-project-id/koyorina/koyorina`）。
`PROJECT_ID` はCloud Buildの組み込み変数で、`gcloud builds submit --project=...`
で指定したビルド実行先のプロジェクトが自動的に入る（`_PROJECT_ID` という
別のカスタム変数を渡す必要はない）。

`_REPOSITORY` は Artifact Registry のリポジトリ名で、
[`setup/gcp/terraform/registry.tf`](../setup/gcp/terraform/registry.tf) が作る
Artifact Registry（platform）の `repository_id`（= `var.name`、Terraform側の
アプリ名変数）と揃える必要がある。`APP_NAME` を変えたときは、こちらも
`--substitutions=_REPOSITORY=...` か `substitutions:` の既定値を合わせて変えること。

| イメージ | Dockerfile | コンテキスト |
|---|---|---|
| `${_APP_NAME}` | Dockerfile | リポジトリルート |
| `${_APP_NAME}-agent` | setup/manifest/Dockerfile.agent | リポジトリルート |
| `${_APP_NAME}-preview-runtime` | setup/preview/Dockerfile.runtime | setup/preview |

- 各イメージにビルド ID と `latest` を付ける。  
- 3本ともビルド後、`images` で push し、Cloud Build の結果に digest を記録する。  
- seccomp プロファイルはイメージにしない。
- setup/ansible のnode-profilesロールがノードへ置く。

任意のタグにする場合:

```powershell
gcloud builds submit . --project=your-gcp-project-id --region=asia-northeast1 --config=cloudbuild.yaml '--substitutions=_TAG=release-20260916-01'
```

完了時に表示されたビルド ID で結果を取得する:

```powershell
gcloud builds describe BUILD_ID --project=your-gcp-project-id --region=asia-northeast1 '--format=yaml(results.images)'
```

配備先では `IMAGE_NAME@sha256:...` に固定する。管理アプリ、コントローラー、
DB 移行には `${_APP_NAME}`、生成ワーカーには `${_APP_NAME}-agent`、
プレビュー実行環境には `${_APP_NAME}-preview-runtime` を利用する。

`.gcloudignore` は Terraform の状態・変数・plan、環境ファイル、ローカル依存などを除外する。
