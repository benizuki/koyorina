---
name: fastapi-backend
description: Python + FastAPI でバックエンド API を書くためのスキル。エンドポイントやルータを追加する、環境変数の設定を整理する、業務ロジックを domain 層に切り出す、JSON/JSONL でデータを永続化する、pytest でテストを書く、SPA を配信する、セキュリティヘッダーや CSP を設定する、といったときに必ず参照すること。「API を足して」「保存できるようにして」「バックエンドを直して」「エラーになるので直して」のように FastAPI という単語が出ていなくても、この構成の Python バックエンドを触るなら使う。プロジェクト全体の構成は webapp-scaffold、ログイン・権限・監査ログは webapp-auth-security、画面側は vue-vuetify-frontend を参照する。
---

# FastAPI バックエンド

SPA を配信しつつ JSON API を返す、単一プロセスのバックエンド構成。

## 層の分け方

```
backend/
├── main.py       # アプリの組み立てだけ。ミドルウェア、ルータ登録、SPA フォールバック
├── config/       # 環境変数を Settings クラス 1 つに集約
├── core/         # 横断的な部品（認証、セキュリティヘッダー、共通ヘルパ、監査ログ）
├── api/          # HTTP ルータ。1 機能 1 ファイル
├── domain/       # 業務ロジック。FastAPI に依存しない
├── data/         # 永続データ（gitignore）
└── tests/
```

**`api/` と `domain/` を分ける理由**が、この構成でいちばん効く。
`api/` は「入力を検証 → `domain` を呼ぶ → JSON にする」だけに留める。
そうすると業務ロジックのテストが `TestClient` なしのただの関数呼び出しになり、
テストが速く、書く気になる。逆に `api/` に業務ロジックを書くと、
テストのたびにアプリ全体を起動することになり、結局テストが書かれなくなる。

```python
# api/orders_api.py — 薄く保つ
@router.post("/api/orders")
async def api_create_order(request: Request):
    actor = require_login(request)
    payload = await _request_json(request)
    try:
        order = orders.create_order(payload.get("items"), actor_email=actor["email"])
    except ValueError as exc:
        return _json_error(str(exc), 400)
    return JSONResponse(status_code=201, content=order)
```

```python
# domain/orders.py — FastAPI を import しない
def create_order(items: list | None, *, actor_email: str) -> OrderRecord:
    if not items:
        raise ValueError("注文明細が空です。1 件以上指定してください。")
    ...
```

`domain` 側は例外に `ValueError` を使い、HTTP のステータスコードは `api` 側で決める。
`domain` が `HTTPException` を投げ始めると、バッチや MCP など HTTP 以外から呼べなくなる。

## 仕様書から書き起こす

`docs/app-spec.md`（`webapp-scaffold` のヒアリングで作る）があるなら、
エンティティの項目表がそのまま `domain/` の検証になる。

| 仕様書 | domain 側 |
|---|---|
| `必須` に ✓ | 空なら `ValueError` を投げる |
| `enum(A,B,C)` | `Literal["A","B","C"]`。候補外なら `ValueError` |
| `ref(entity)` / `user` | 参照先に存在するかを確認する |
| 備考の「一意」 | 保存前に重複を確認する |
| 備考の「既定=〇〇」 | 未指定なら埋める |

`id` / `created_at` / `created_by` / `updated_at` / `updated_by` は
仕様書に書かれていなくても**全エンティティに付ける**。
後から足すと既存データに値が無く、移行が要る。

**フロントの検証は入力補助であって防御ではない。** 同じ検証をここに書く。

## 立ち上げ

```bash
SKILL_DIR="${APP_FORGE_SKILLS:-$HOME/.claude/skills}/fastapi-backend"
mkdir -p backend && cp -R "$SKILL_DIR/assets/." backend/
cd backend && pip install -r requirements.txt
cp .env.sample .env   # 値を埋める
python main.py
```

同梱物: `main.py` / `requirements.txt` / `.env.sample` / `config/settings.py` /
`core/api_common.py` / `core/security_headers.py` / `api/general_api.py` /
`pytest.ini` / `tests/test_example.py` / `tests/test_security_headers.py`

**`main.py` と `api/general_api.py` は `core/auth.py` を import する。**
そのファイルは `webapp-auth-security` スキルの `assets/backend/core/` にあるので、
先にそちらを配置すること。ログインを付けないアプリにするなら、
`init_oauth` / `auth_router` / `require_login` の呼び出しを消してから起動する。

## main.py の組み立て順

ミドルウェアの順序で挙動が変わるので、順番には意味がある。

1. `FastAPI()` を作る
2. `SecurityHeadersMiddleware` — **いちばん先に**入れる。ミドルウェアは後から足したものほど
   外側に来るので、最初に登録すると、他のミドルウェアが返すエラー応答にもヘッダーが付く
3. `init_oauth(app)` — SessionMiddleware。**認証系ルータより先に**入れる
4. 認証ルータ（`/login` `/auth/*` `/logout`）
5. CORS ミドルウェア
6. 業務ルータ（`/api/*`）
7. 例外ハンドラ
8. `/healthz` — 認証を掛けない唯一のエンドポイント。
   ロードバランサのヘルスチェックがここを叩く。302 を返すと `UNHEALTHY` になる
9. **SPA フォールバックを最後に**登録する

Koyorinaではプレビュー前段が画面を配信するが、`GET /` が404になるバックエンドは
完成扱いにしない。ローカル実行や公開用コンテナでも同じ入口が使えるよう、`GET /` を
明示するか、最後のSPAフォールバックで必ず `index.html`（または有効なリダイレクト）を返す。
`frontend/dist` がまだ無い場合は、何を実行すべきか分かる固定エラーを返す。

SPA フォールバックは `@app.get("/{path:path}")` なので、
先に登録すると全ルートを食ってしまう。必ず最後に置く。

```python
@app.get("/{path:path}")
def spa_fallback(path: str):
    # API 相当のパスは 404 を返す。SPA の index.html を返すと、
    # フロントが「HTML が返ってきた」で JSON パースに失敗して原因が分かりにくくなる。
    if path.startswith(("api/", "auth/", "login", "logout", "healthz")):
        return _json_error("not found", 404)

    asset_path = FRONTEND_DIST_DIR / path
    if path and asset_path.is_file():
        return FileResponse(asset_path)
    return _frontend_index()
```

## エラーの返し方

レスポンスの形を 1 つに揃える。フロント側の分岐が減り、扱いを間違えにくくなる。

```json
{ "error": "利用者向けの日本語メッセージ", "status_code": 403 }
```

そのために、`HTTPException` を一箇所で JSON に変換する。

```python
@app.exception_handler(HTTPException)
async def handle_http_exception(_: Request, exc: HTTPException):
    return http_exception_to_response(exc)

@app.exception_handler(Exception)
async def handle_exception(_: Request, exc: Exception):
    # 例外はサーバのログに全文を残し、利用者には型名までに留める。
    # スタックトレースを返すと内部構造が漏れる。
    logger.exception("unexpected error")
    return JSONResponse(
        status_code=500,
        content={"error": f"{type(exc).__name__}: {exc}", "status_code": 500},
    )
```

`detail` に dict を渡すと追加情報を載せられる。利用者への案内に使う。

```python
raise HTTPException(
    status_code=403,
    detail={"error": "この操作には編集権限が必要です。管理者に依頼してください。",
            "your_role": current_role(request)},
)
```

## セキュリティヘッダー

`core/security_headers.py` を `main.py` の**いちばん先に**登録する。
雛形には最初から入れてある。

```python
from core.security_headers import SecurityHeadersMiddleware

app.add_middleware(SecurityHeadersMiddleware)
```

これだけで、XSS の被害範囲・クリックジャッキング・MIME スニッフィング・
リファラ経由の情報漏れを、アプリのコードに手を入れずに抑えられる。

**最初から入れること。** 後付けにすると、すでに動いている画面が CSP でまとめて壊れ、
どの機能がどのディレクティブに引っかかっているのかを 1 つずつ突き止める作業になる。
最初から入れておけば、壊れるのは「いま書いた 1 画面」だけで済む。

| ヘッダー | 何を止めるか |
|---|---|
| `Content-Security-Policy` | 許可していない出所のスクリプト・接続。XSS を踏んでも被害が広がりにくくなる |
| `X-Content-Type-Options: nosniff` | 宣言と違う型での解釈。アップロードしたテキストが HTML として実行されるのを防ぐ |
| `X-Frame-Options: DENY` | 他サイトの iframe への埋め込み（クリックジャッキング） |
| `Referrer-Policy` | 外部サイトへ遷移するときのパス・クエリの漏れ |
| `Permissions-Policy` | カメラ・マイク・位置情報などの利用 |
| `Strict-Transport-Security` | HTTPS のときだけ付く。http の開発環境では付けない |

### 外部サービスを呼ぶ機能を足したとき

CSP の該当ディレクティブに追記する。忘れると、ブラウザのコンソールに CSP 違反として出る。
画面上は**「読み込み中のまま止まる」ように見える**ことが多いので、まずそこを疑う。

| 足したもの | 追記先 |
|---|---|
| 外部 API を `fetch` する | `connect-src` |
| 画像を外部から読む | `img-src` |
| 外部のスクリプトを読む | `script-src` |
| Web フォントを追加する | `font-src` と `style-src` |

恒久的に必要なものは `security_headers.py` の定数に直接書いて、履歴に残す。
一時的な検証は環境変数（`CSP_EXTRA_CONNECT_SRC` など）でも足せるようにしてあるが、
常用しないこと。設定にしか書かれていない許可は、レビューで見つからない。

**`script-src` に `'unsafe-inline'` や `'unsafe-eval'` を足さない。**
足した瞬間に、CSP による XSS の緩和がほぼ意味を失う。
`style-src` の `'unsafe-inline'` は、Vuetify が要素へ直接 `style` 属性を書き込むため許容している。
`tests/test_security_headers.py` がこの区別を検査するので、緩めるとテストが落ちる。

### キャッシュ制御も同じミドルウェアで付ける

パスごとに分けている。ここを間違えると、原因の分かりにくい不具合になる。

- `/api/*` → `no-store`。権限を変えた直後に古い応答が返るのを防ぐ
- `/assets/*` → `immutable`。Vite の出力はファイル名にハッシュが入るので長く持たせてよい
- HTML → `must-revalidate`。ここをキャッシュさせると、デプロイしても利用者に古い SPA が出続ける

**ロードバランサ側でヘッダーを付けないこと。** 2 か所で付けると、
どちらが効いているのか分からなくなり、CSP を直したのに反映されない状態になる。
アプリ側に寄せておけば、ローカルで確認したものがそのまま本番に出る（`gce-cos-deploy` 参照）。

## 設定（config/settings.py）

環境変数の読み出しは全部ここに集める。`os.getenv` をコードのあちこちに書くと、
必要な設定の全体像が誰にも分からなくなり、デプロイのたびに設定漏れで落ちる。

```python
class Settings:
    # 無いと動かないものは os.environ で。起動時に落として気付かせる。
    project: str = os.environ["GOOGLE_CLOUD_PROJECT"]

    # 任意のものはヘルパで既定値を持たせる。
    feature_x_enabled: bool = _env_flag("FEATURE_X_ENABLED", default=False)
    data_dir: str = _env_str("DATA_DIR", default="/app/backend/data")
    allowed_origins: tuple[str, ...] = _env_csv("CORS_ALLOWED_ORIGINS")

@lru_cache
def get_settings() -> Settings:
    return Settings()
```

`_env_flag` / `_env_str` / `_env_csv` は複数のキー名を順に試せるようにしてある
（`_env_str("NEW_NAME", "OLD_NAME", default=...)`）。環境変数をリネームするときに、
移行期間中は両方を受け付けられる。

環境変数を足したら **`.env.sample` と `env.yaml` を同じ変更の中で更新する**。
ここを別 PR に回すと、ほぼ確実に忘れる。

## 永続データ

小規模のうちは `data/` 配下のファイルで十分。DB を建てる手間と運用を後回しにできる。

| 用途 | 形式 | 例 |
|---|---|---|
| マスタ・設定（少件数、全件読む） | `.json` | ユーザー一覧、グループ定義 |
| 追記される記録（件数が伸びる） | `.jsonl` | 監査ログ、実行履歴 |

JSON ファイルの更新は「読む → 変える → 全部書く」を 1 つのロックで囲む。

```python
_lock = threading.Lock()

def upsert_record(record: dict) -> dict:
    with _lock:
        records = _load()
        idx = next((i for i, r in enumerate(records) if r["id"] == record["id"]), None)
        if idx is None:
            records.append(record)
        else:
            records[idx] = record
        _save(records)
    return record
```

**この方式の限界を意識すること。** `threading.Lock` は 1 プロセス内でしか効かない。
gunicorn の worker を増やす、Cloud Run のインスタンスが複数立つ、
k3s のレプリカを 2 にする、のいずれかをやった瞬間に書き込みが壊れる。
そうなる見込みが出たら、Firestore や Cloud SQL に移す。

JSONL はサイズでローテートする。放置すると読み出しが重くなり、
ディスクも食う。実装は `webapp-auth-security` の `core/auth_audit.py` にある。

## 非同期と同期

FastAPI は `def` と `async def` を混在できる。判断基準はこれだけ。

- **`async def`** — `await` するものがある（`await request.json()`、非同期クライアント）
- **`def`** — 同期ライブラリを呼ぶ（BigQuery クライアント、ファイル IO）。
  FastAPI がスレッドプールで実行してくれるので、イベントループを塞がない

**`async def` の中で同期のブロッキング呼び出しをしない。**
これをやると全リクエストが止まる。どうしても必要なら
`await asyncio.to_thread(blocking_call, ...)` で逃がす。

## テスト

`pytest` を使う。`domain/` の関数を直接呼ぶテストを主軸にする。

```python
import domain.orders as orders

def test_create_order_rejects_empty_items():
    with pytest.raises(ValueError, match="注文明細"):
        orders.create_order([], actor_email="a@example.com")
```

**永続ファイルを汚さないよう、保存先を必ず差し替える。**
テストを走らせたら `data/` の中身が消えていた、という事故が起きやすい。

```python
@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    monkeypatch.setattr(orders, "_STORE_PATH", tmp_path / "orders.json")
```

HTTP 層のテストが要るときだけ `TestClient` を使う。
認証必須のエンドポイントは、`require_login` を依存性注入で差し替えるか、
セッション Cookie を直接組み立てる。詳細は `references/testing.md`。

## 参照ファイル

- `references/api-patterns.md` — ルータの型、バリデーション、長時間処理、外部 API 呼び出し
- `references/testing.md` — pytest の構成、フィクスチャ、認証付きエンドポイントのテスト
