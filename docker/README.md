# イメージのビルド材料

ここにはコンテナイメージに焼き込むものだけを置く。クラスタやサーバーへの
セットアップ（マニフェスト・Helm・Ansible・配備スクリプト）は `setup/` に置く。
ここを変えたときは、イメージをビルドして push し直す（`make build-push` か Cloud Build）。

| フォルダ | イメージ | 中身 |
|---|---|---|
| （ルートの `Dockerfile`） | `koyorina` | 管理アプリ本体。`publication/` もこのイメージに入る |
| `agent/` | `koyorina-agent` | 生成エージェント。`codex-bwrap-no-proc` は `/usr/local/bin/bwrap` として入る |
| `preview-runtime/` | `koyorina-preview-runtime` | プレビュー実行環境。このフォルダ自体がビルドコンテキスト |
| `publication/` | （管理アプリに同梱） | 公開アプリのイメージの雛形。公開コントローラーが `/app/docker/publication` から読む |

`publication/front.py` と `preview-runtime/front.py` は同じ内容に保つ
（`backend/tests/test_preview_front.py` が確かめる）。
