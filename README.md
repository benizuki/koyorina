# Koyorina — AIアプリ開発を体験するためのアプリ

Koyorinaは、AIを使ったWebアプリ開発を体験するためのアプリケーションです。
対話形式で仕様を作り、CodexやVertex AI Geminiなどの生成エンジンにコードを生成させ、
生成したアプリをKubernetes上で確認できます。ITやAIに馴染みのない人が、仕様作成から
コード生成、アプリ実行までを体験できることを目的にしています。

現在は学習・検証向けの開発中ソフトウェアです。外部公開や本番運用を前提とした安全性、
可用性、運用手順は保証していません。まずはlocalhostまたは閉じた検証環境で利用してください。

## AIを使ったアプリ作成で体験できること

- AIとの対話からWebアプリの仕様を作る方法
- 作成されたアプリの実行と動作確認

[実際の画面でアプリ生成の流れを見る](docs/app-generation-flow.html)

動作確認には、製造・バックオフィス向けの[サンプルデータ](sample-test-data/README.md)を
利用できます。CSV、TSV、Excel、帳票PDFを収録しています。

## まず試す（Docker Compose）

k3sを用意しなくても、Docker（Docker Desktop等）だけで、仕様作成 → コード生成 →
生成アプリのプレビューと公開まで一通り試せます。

```sh
docker compose up -d --build
```

初回はイメージのビルドに数分かかります。終わったら **http://localhost:8080** を開きます
（`127.0.0.1` ではなく `localhost`）。ログインは不要です。8080番が使用中なら
`.env` に `COMPOSE_APP_PORT=8081` を設定し、`http://localhost:8081` を開いてください。

生成AIは画面の「マスター管理 → システム設定 → 生成AIの設定」で選び、APIキーもそこで
入れます（本番構成と同じ画面）。保存すると、生成中でなければ数秒でエージェントが
新しい設定で起動し直します。

- **Codex**: 画面の「Codexへログイン」から本人のChatGPTアカウントに接続します
- **Gemini**: Gemini API（Google AI StudioのAPIキー）
- **Ollama等のOpenAI互換API / Claude**: 接続先・モデル・APIキー。ホストのOllamaは
  `http://host.docker.internal:11434/v1`

止めるときは `docker compose down`（データは残ります）。ログは
`docker compose logs -f app codex-controller agent`。`make compose-up` / `compose-down` /
`compose-logs` も同じことをします（`compose-down` は起動中のプレビューも片付けます）。

Compose版でも、生成版を「公開」タブからビルド・Pushして公開アプリ運用画面で起動できます。
ローカルRegistryは `127.0.0.1:5000` だけに公開し、イメージと公開データはそれぞれ
`registry-data`、Dockerの名前付きボリュームに保存します。`docker compose down` では保持され、
公開アプリの削除ではそのアプリのデータボリュームを消します。`docker compose down -v` は
Registryと公開記録のボリュームを消しますが、個別アプリのデータボリュームは残るため、
先に公開アプリ運用から削除してください。Compose版は単一ホストの
お試し構成で、公開アプリ用のCPU・メモリ上限は適用しますが、ストレージ容量はDockerの
名前付きボリュームに上限を設定しません。

変えたい設定があれば `compose.env.example` を `.env` にコピーして書きます（省略可）。
Linuxでは `DOCKER_SOCKET_GID` に `stat -c %g /var/run/docker.sock` の値を入れてください
（プレビューの起動に使います。Docker Desktopは不要）。

内部の鍵（サービス間の認証鍵、APIキーを暗号化する鍵）は初回起動時に作られ、Dockerの
ボリュームに置かれます。**`docker compose down -v` でボリュームごと消すと、保存した
APIキーは開けなくなります**（DBも消えるので、作り直しと同じです）。

k3s版との違い:

- **1人用・ログインなし・localhostのみ**です。他の人に公開しないでください
- テナントの移行・容量計測・Vertex AI（ADC/Workload Identity）は使えません
- 生成は常駐のエージェント1つで行います（k3s版は利用者・テナントごとに分離）
- プレビューを起動するため、管理アプリにDockerのソケットを渡しています。
  これはホストのroot相当の権限です。信頼できない環境では動かさないでください

## デプロイ

デプロイ作業は、特記があるものを除いてローカルPCから実行します。必要な設定値と
実行コマンドは、配備先ごとの手順にまとめています。

| 配備先 | 手順 |
|---|---|
| オンプレミスのk3s | [オンプレミスサーバ k3s への配備](docs/deployment/onprem-k3s.md) |
| Google Compute Engine | [GCE への配備](docs/deployment/gce.md) |

Ansibleのファイル構成や設定項目は[Ansibleの解説](setup/ansible/README.md)、
ストレージ方式の選び方は[ストレージ構成](docs/deployment/storage.md)を参照してください。

秘密情報と認証情報の扱いは[Secretの管理](docs/deployment/secrets.md)を参照してください。

生成アプリのビルド・Pushと利用者への公開は[公開機能の配備手順](docs/deployment/publication.md)を参照してください。

## 免責事項

本ソフトウェアは学習・検証を目的として、現状のまま提供されます。正確性、完全性、
安全性、可用性、特定目的への適合性を保証しません。本ソフトウェアおよび生成されたコードの
利用、変更、公開は利用者自身の責任で行ってください。

適用法令で認められる範囲において、開発者およびコントリビューターは、本ソフトウェアの
利用または利用不能によって生じた損害について責任を負いません。外部の生成AIやクラウドを
利用する場合は、各サービスの利用規約、データの取り扱い、料金を利用者が確認してください。

## ライセンス

[Apache License 2.0](LICENSE)
