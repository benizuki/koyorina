# デプロイ

## 目次

- [ローカルで本番相当を確認する](#ローカルで本番相当を確認する)
- [Cloud Run](#cloud-run)
- [自前 k3s / Kubernetes](#自前-k3s--kubernetes)
- [社内レジストリとプライベート CA](#社内レジストリとプライベート-ca)
- [デプロイ前チェックリスト](#デプロイ前チェックリスト)

## ローカルで本番相当を確認する

```bash
docker build -t myapp:dev .
docker run --rm -p 8080:8080 --env-file backend/.env myapp:dev
```

`--env-file` を使うと `.env` の値がそのまま入る。Google ログインを試すなら、
Google Cloud Console の「承認済みの JavaScript 生成元」に `http://localhost:8080` を
**完全一致で**登録しておく。`localhost` と `127.0.0.1`、`8080` と `5173` は別 Origin として扱われる。

## Cloud Run

`cloudbuild.yaml` を使う。

```bash
gcloud builds submit --config cloudbuild.yaml --substitutions=_TAG=$(git rev-parse --short HEAD)
```

### 事前に用意するもの

1. **Artifact Registry のリポジトリ**
   ```bash
   gcloud artifacts repositories create my-repo --repository-format=docker --location=asia-northeast1
   ```
2. **セッション鍵を Secret Manager に登録**
   ```bash
   python -c "import secrets; print(secrets.token_urlsafe(64))" | \
     gcloud secrets create myapp-session-secret --data-file=-
   ```
   この鍵が変わると全ユーザーのセッションが無効になる。ローテーションは意図して行う。
3. **サービスアカウント** — アプリが使う GCP API（BigQuery、Logging など）の権限だけを付ける。
   Cloud Run の既定 SA（Compute Engine SA）は権限が広すぎるので使わない。
4. **永続データ用の GCS バケット** — `--add-volume` でマウントする。

### Cloud Run 特有の注意

- **書き込めるのはマウントしたボリュームだけ。** コンテナのファイルシステムは
  再起動で消える。`backend/data/` を必ずマウント先にする。
- **GCS の FUSE マウントは追記が遅い。** JSONL の監査ログを大量に書くなら、
  Cloud Logging に構造化ログとして出す方式に寄せる（`webapp-auth-security` 参照）。
- **インスタンスは複数起動しうる。** ファイル保存でロックを取っていても、
  別インスタンス間では効かない。`--max-instances=1` にするか、状態を外部サービスに出す。
- **タイムアウト。** 既定 300 秒。長い処理があるなら `--timeout=900` と gunicorn の
  `--timeout` を両方伸ばす。片方だけだと 502 になる。

## 自前 k3s / Kubernetes

以下を `deploy/k3s/<app-name>.yaml` として置く。`__APP_NAME__` `__NAMESPACE__`
`__IMAGE__` を置換して使う。

```yaml
apiVersion: v1
kind: Namespace
metadata:
  name: __NAMESPACE__
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: __APP_NAME__-config
  namespace: __NAMESPACE__
data:
  # 秘密でない環境変数だけをここに置く。env.yaml と同じ内容を保つこと。
  GOOGLE_CLOUD_PROJECT: "my-project"
  CORS_ALLOWED_ORIGINS: "https://__APP_NAME__.example.com"
  AUTH_AUDIT_LOG_PATH: "/app/backend/data/auth_audit_logs.jsonl"
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: __APP_NAME__-data
  namespace: __NAMESPACE__
spec:
  accessModes: [ReadWriteOnce]
  storageClassName: local-path
  resources:
    requests:
      storage: 10Gi
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: __APP_NAME__
  namespace: __NAMESPACE__
spec:
  # ユーザーマスタとジョブ状態を PVC に置いている間は 1 レプリカのまま。
  # 共有ストレージに移すまで増やすと、書き込みが競合して壊れる。
  replicas: 1
  strategy:
    type: Recreate
  selector:
    matchLabels:
      app.kubernetes.io/name: __APP_NAME__
  template:
    metadata:
      labels:
        app.kubernetes.io/name: __APP_NAME__
    spec:
      initContainers:
        # イメージに同梱した初期データを、空の PVC に一度だけ展開する。
        - name: initialize-data
          image: __IMAGE__
          command: ["/bin/sh", "-ec"]
          args:
            - |
              if [ ! -e /persistent-data/.initialized ]; then
                cp -a /app/backend/data/. /persistent-data/
                touch /persistent-data/.initialized
              fi
          volumeMounts:
            - name: application-data
              mountPath: /persistent-data
      containers:
        - name: app
          image: __IMAGE__
          imagePullPolicy: Always
          ports:
            - name: http
              containerPort: 8080
          envFrom:
            - configMapRef:
                name: __APP_NAME__-config
          env:
            - name: APP_SESSION_SECRET
              valueFrom:
                secretKeyRef: { name: __APP_NAME__-secrets, key: APP_SESSION_SECRET }
            - name: GOOGLE_OAUTH_CLIENT_ID
              valueFrom:
                secretKeyRef: { name: __APP_NAME__-secrets, key: GOOGLE_OAUTH_CLIENT_ID }
            - name: GOOGLE_APPLICATION_CREDENTIALS
              value: /var/run/secrets/gcp/service-account.json
          readinessProbe:
            httpGet: { path: /healthz, port: http }
            initialDelaySeconds: 5
            periodSeconds: 10
          livenessProbe:
            httpGet: { path: /healthz, port: http }
            initialDelaySeconds: 20
            periodSeconds: 20
          startupProbe:
            # 起動が遅いアプリでも liveness に殺されないよう、startupProbe を必ず置く。
            httpGet: { path: /healthz, port: http }
            failureThreshold: 30
            periodSeconds: 10
          resources:
            requests: { cpu: 500m, memory: 1Gi }
            limits:   { cpu: "2",  memory: 4Gi }
          volumeMounts:
            - name: application-data
              mountPath: /app/backend/data
            - name: gcp-service-account
              mountPath: /var/run/secrets/gcp
              readOnly: true
      volumes:
        - name: application-data
          persistentVolumeClaim:
            claimName: __APP_NAME__-data
        - name: gcp-service-account
          secret:
            secretName: gcp-service-account
            items:
              - key: service-account.json
                path: service-account.json
---
apiVersion: v1
kind: Service
metadata:
  name: __APP_NAME__
  namespace: __NAMESPACE__
spec:
  selector:
    app.kubernetes.io/name: __APP_NAME__
  ports:
    - name: http
      port: 80
      targetPort: http
```

Secret の作成:

```bash
kubectl -n __NAMESPACE__ create secret generic __APP_NAME__-secrets \
  --from-literal=APP_SESSION_SECRET="$(python -c 'import secrets;print(secrets.token_urlsafe(64))')" \
  --from-literal=GOOGLE_OAUTH_CLIENT_ID="xxxx.apps.googleusercontent.com"
```

Ingress は環境によるので、既存の Ingress/Gateway に合わせて別ファイルに書く。
TLS 終端は Ingress 側で行い、アプリは HTTP のまま受ける。
その場合、`X-Forwarded-For` を信頼できるのは Ingress が付けたものだけなので、
クライアント IP を監査ログに残すときは先頭要素だけを見ること。

## 社内レジストリとプライベート CA

社内レジストリがプライベート CA の証明書を使っていると、`docker buildx` は
そのままでは push できない。BuildKit の設定ファイルを用意し、builder を作り直す。

```toml
# ~/buildkitd-registry.toml
[registry."registry.example.com"]
  ca = ["/etc/docker/certs.d/registry.example.com/ca.crt"]
```

```bash
make recreate-builder BUILDKITD_CONFIG=$HOME/buildkitd-registry.toml
make build-push TAG=$(git rev-parse --short HEAD)
```

BuildKit の設定は builder を**作るときにしか**読まれない。
CA を足したのに反映されないときは、まず `recreate-builder` を疑う。

## デプロイ前チェックリスト

- [ ] `APP_SESSION_SECRET` が Secret 経由で入っていて、開発用の値のままでない
- [ ] `GOOGLE_OAUTH_CLIENT_ID` が本番用で、Console の承認済み Origin に本番 URL が入っている
- [ ] `CORS_ALLOWED_ORIGINS` が本番 Origin だけになっている（`*` になっていない）
- [ ] 永続データのマウント先が `/app/backend/data` になっている
- [ ] ユーザーマスタに管理者アカウントが 1 件以上入っている（入れ忘れると誰も入れない）
- [ ] `/healthz` が 200 を返す
- [ ] `.env` や秘密がイメージに入っていない（`.dockerignore` を確認）
