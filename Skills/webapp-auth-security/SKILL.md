---
name: webapp-auth-security
description: 社内 Web アプリに Google アカウントのログイン、開発時だけログインを省く仕組み、ロールと権限（RBAC）、ユーザー管理画面、認証・操作の監査ログ、そのほかのセキュリティ対策（セッション、CORS、HTTP セキュリティヘッダー / CSP、秘密情報の扱い、入力検証、XSS/CSRF/SSRF 対策）を実装するためのスキル。「ログインを付けたい」「特定の人だけ使えるようにしたい」「管理者だけ操作できるようにして」「誰が何をしたか記録したい」「セキュリティ的に問題ないか見て」「認証が効かない」「開発中はログインを省きたい」「ローカルでは認証なしで動かしたい」のように、認証・認可・権限・監査・セキュリティに関わる話が出たら必ず参照すること。新しく作るアプリでも既存アプリへの後付けでも使う。全体構成は webapp-scaffold、API の書き方は fastapi-backend、画面側は vue-vuetify-frontend を参照する。
---

# 認証・認可・監査（Google ログイン + セッション + RBAC）

社内 Web アプリを「許可した人だけが、許可された操作だけできる」状態にし、
後から誰が何をしたか追えるようにするための構成。

## 全体像

```
ブラウザ                        バックエンド
   │
   │ 1. GET /                   未ログインなら 302 → /login
   │ 2. GET /login              Google Identity Services のボタンを描いた HTML
   │ 3. Google でサインイン      ID トークン（JWT）がブラウザに返る
   │ 4. POST /auth/google       ID トークンを送る
   │                            ├ Google の公開鍵で署名を検証
   │                            ├ aud（クライアント ID）と iss を検証
   │                            ├ email_verified を確認
   │                            ├ ユーザーマスタに居るか・有効かを確認  ← 認可の入口
   │                            └ セッションに user / role / permissions を保存
   │ 5. 以降は Cookie で認証     require_login() / require_permission() で検査
```

**「Google で認証できること」と「このアプリを使ってよいこと」は別物**、
というのがこの設計の要。Google アカウントを持っている人は世界中にいるので、
署名検証を通っただけでは通してはいけない。自前のユーザーマスタに
登録されていて `enabled` が真であることを、必ずもう一段確認する。

## なぜトークンではなくセッション Cookie か

SPA だと JWT を localStorage に持たせる作りをよく見るが、この構成では採らない。

- localStorage の値は **XSS を踏んだ瞬間に全部読まれる**。`HttpOnly` Cookie なら読めない
- セッションなら、ユーザーを無効化したときに次のリクエストから弾ける。
  自前で失効させにくい JWT より、社内ツールの運用に合う
- SPA 側に認証ロジックを一切持たなくて済む。フロントは `/api/me` を呼ぶだけ

代償として CSRF を自分で考える必要がある。対策は下の
[CSRF](#csrf) と `references/security-checklist.md` を参照。

## 導入手順

### 1. ファイルを配置する

```bash
SKILL_DIR="${APP_FORGE_SKILLS:-$HOME/.claude/skills}/webapp-auth-security"
cp "$SKILL_DIR/assets/backend/core/"*.py            backend/core/
cp "$SKILL_DIR/assets/backend/api/users_api.py"     backend/api/
cp "$SKILL_DIR/assets/backend/templates/login.html" backend/templates/
cp "$SKILL_DIR/assets/backend/tests/test_dev_auth.py" backend/tests/
cp "$SKILL_DIR/assets/frontend/useAuth.ts"          frontend/src/composables/
```

| ファイル | 役割 |
|---|---|
| `core/auth.py` | ログイン、セッション、権限検査のヘルパ |
| `core/dev_auth.py` | 開発時（`APP_ENV=local`）だけログインを省く。安全弁もここ |
| `core/user_store.py` | ユーザーマスタ（`data/users.json`）の読み書きとロール既定値 |
| `core/auth_audit.py` | 認証・操作の監査ログ（JSONL、ローテート付き） |
| `api/users_api.py` | ユーザー管理 API（管理者のみ） |
| `templates/login.html` | Google Identity Services のログイン画面 |
| `useAuth.ts` | フロント側のログイン状態・権限の取得 |
| `tests/test_dev_auth.py` | 認証スキップが本番で有効にならないことの検査 |

`core/security_headers.py` はこのスキルには入っていない。`fastapi-backend` の
`assets/core/` にあるので、そちらから配置する。**CSP に Google のオリジンを
許可しないとログイン画面が動かない**ので、先に入れておくこと（下の
[HTTP セキュリティヘッダー](#http-セキュリティヘッダー)）。

### 2. 機能キーを決める

`core/user_store.py` の `PermissionRecord` を、アプリに合わせて書き換える。
`docs/app-spec.md` の権限表（`webapp-scaffold` のヒアリングで作る）があるなら、
その機能キーをそのまま写す。
**ロールではなく機能キーで判定する**のがこの設計の肝。
`if role == "admin"` を書き始めると、「この人だけ例外的に許可したい」が来たときに
ロールが増殖して破綻する。

```python
class PermissionRecord(TypedDict):
    can_view:         bool   # 参照
    can_edit:         bool   # 登録・更新
    can_export:       bool   # 出力・ダウンロード
    can_manage_users: bool   # ユーザー管理（これは共通で持たせる）
```

ロールは「機能キーの既定値セット」として扱う。`_permissions_for_role()` を書き換える。
既定は admin（全部）/ power（管理以外）/ general（参照のみ）の 3 段。

### 3. main.py に組み込む

```python
from core.auth import auth_router, current_user, http_exception_to_response, init_oauth
from core.security_headers import SecurityHeadersMiddleware
from api.users_api import router as users_router

app = FastAPI(title="...")
app.add_middleware(SecurityHeadersMiddleware)   # いちばん先。CSP は下記の許可が要る
init_oauth(app)              # SessionMiddleware。認証ルータより先に入れる
app.include_router(auth_router)
app.include_router(users_router)
```

### 4. 環境変数を設定する

```bash
# セッション署名鍵。長いランダム文字列。変えると全員ログアウトになる。
python -c "import secrets; print(secrets.token_urlsafe(64))"
```

```
APP_SESSION_SECRET=<上で生成した値>
GOOGLE_OAUTH_CLIENT_ID=xxxxxxxx-yyyy.apps.googleusercontent.com
```

Google Cloud Console で **ウェブアプリケーション**タイプの OAuth クライアント ID を作り、
「承認済みの JavaScript 生成元」に、実際にブラウザで開く Origin を
**完全一致で**登録する。`http://localhost:8080` と `http://127.0.0.1:8080` は別 Origin、
`:8080` と `:5173` も別 Origin として扱われる。ここのミスが初回のつまずきの大半。

### 5. 最初の管理者を登録する

`backend/data/users.json` を手で作る。これを忘れると誰もログインできない。

```json
[
  {
    "email": "admin@example.com",
    "name": "管理者",
    "role": "admin",
    "enabled": true
  }
]
```

以降は画面の「ユーザー管理」から追加できる。

## 開発中はログインを省く

ローカルで画面を触るたびに Google の同意画面を通すのは、開発を確実に遅くする。
OAuth クライアント ID をまだ発行していない段階でも画面を作れるようにしたい。

`backend/.env` に 1 行入れるだけで、Google を通さず固定のユーザーとして扱われる。

```bash
APP_ENV=local
DEV_USER_EMAIL=dev@example.com
DEV_USER_ROLE=general     # admin / power / general
```

`GOOGLE_OAUTH_CLIENT_ID` も `APP_SESSION_SECRET` も未設定でよい。
**設定ゼロで `python main.py` が動く**ので、認証まわりを後回しにして画面から作れる。

**`DEV_USER_ROLE` を `general` や `power` に切り替えて確認すること。**
`admin` のままだと権限で隠れるはずの画面が全部見えてしまい、
「一般ユーザーには操作できない」を作り込めているか確認できない。
`users.json` に `DEV_USER_EMAIL` のユーザーを登録すれば、本番に近い形で個別の権限も試せる。

### 本番で有効にならないための 4 段

**これは認証を丸ごと外す機能なので、本番で有効になったら終わりになる。**
1 つでも通らなければ有効にならないように積んである。

| | 仕組み | 効く場面 |
|---|---|---|
| 1 | **既定は無効。** `APP_ENV` 未設定は production 扱い | 設定し忘れ。fail closed にしてある |
| 2 | クラウド上の実行を検出したら**起動を止める** | `env.yaml` に書いてしまった。Cloud Run / App Engine / k8s の環境変数で判定する |
| 3 | デプロイスクリプトが `APP_ENV=local` を弾く | `cos.env` に書いてしまった（GCE には 2 で使える環境変数が無いため） |
| 4 | 有効な間は**画面上部に常時バナー**が出る（`/api/me` の `auth_mode`） | 気付かないまま使い続ける・デモしてしまう |

2 を警告ログではなく例外にしているのは、**ログは見落とされるが起動失敗は必ず気付かれる**ため。

```
RuntimeError: APP_ENV=local のままクラウド上で起動しようとしています（検出: K_SERVICE）。
認証が無効になるため起動を中止しました。
```

`tests/test_dev_auth.py` が「既定で無効」「local 以外の値では無効」
「クラウド検出で起動拒否」を検査する。**この仕組みを触ったら必ず通すこと。**

**要らないなら `core/dev_auth.py` を消す。** `auth.py` の import と
分岐を外せば、機能ごと無くなる。本番専用のリポジトリに切り出すときはそうする。

## エンドポイントの守り方

```python
@router.get("/api/orders")
def api_orders_list(request: Request):
    require_login(request)                      # 401（未ログイン）
    require_permission(request, "can_view")     # 403（権限なし）
    return {"orders": orders.list_orders()}
```

**全エンドポイントに `require_login` を書く。** 「見られても困らないから」で
外し始めると、後から機微な情報が乗ったときに漏れる。例外は `/healthz` だけ
（プローブが 302 を受け取ると死活監視が壊れるため）。

**フロントでボタンを隠すのは体験のためであって、防御ではない。**
利用者は URL を直接叩けるし、開発者ツールから API も叩ける。
UI の出し分けとバックエンドの検査は必ず両方書く。

## 監査ログ

「誰が」「いつ」「何をしたか」を JSONL に残す。3 種類のイベントを記録する。

| カテゴリ | いつ | 用途 |
|---|---|---|
| `authentication` | ログイン成功・失敗 | 不正アクセスの検知、退職者アカウントの利用 |
| `authorization` | ユーザー・権限の変更 | 権限がいつ誰に付いたかの追跡 |
| （アプリ固有） | 重要な業務操作 | 「消したのは誰か」に答える |

ログイン失敗は**理由まで記録する**。「毎回同じ IP から未登録メールで失敗し続けている」
のようなパターンが見えるようにするため。詳細は `references/audit-log.md`。

個人情報の扱いに注意する。**アプリケーションログにはメールアドレスを載せず、
ハッシュ（`user_id_hash`）を使う。** 監査ログだけは追跡のため生のメールを持つが、
その代わり閲覧を `can_manage_users` に限定する。

## セキュリティ上の要点

詳細と確認手順は `references/security-checklist.md`。ここでは頻出のものだけ。

### セッション

- `APP_SESSION_SECRET` は環境変数（本番は Secret Manager / k8s Secret）から読む。
  コードに直書きしない。漏れたら**任意のユーザーになりすませる**。
  GCE COS では `APP_SECRET_ENV` 経由で VM が起動時に読む（`gce-cos-deploy` 参照）
- セッションに **OAuth クライアント ID の指紋を入れて突き合わせる**。
  クライアント ID を差し替えたのに古いセッションが生き続ける、を防ぐ
  （`core/auth.py` の `_session_is_valid`）
- ログアウトは `request.session.clear()`。個別キーの削除は消し漏れる

### CSRF

セッション Cookie 認証なので CSRF を考える必要がある。この構成では:

- **Cookie の `SameSite=Lax`**（Starlette の SessionMiddleware の既定）で、
  他サイトからの POST にはセッションが付かない
- **状態を変える操作は必ず POST / PUT / DELETE**。GET で削除できるようにしない
- **`Content-Type: application/json` を要求する**。単純リクエストの form POST では
  JSON を送れないため、フォームからの CSRF が成立しない

`SameSite=None` が必要になる構成（別ドメインの iframe に埋めるなど）にするなら、
CSRF トークンを別途導入する。安易に緩めない。

### HTTP セキュリティヘッダー

`fastapi-backend` の `core/security_headers.py` を `main.py` のいちばん先に登録する。
**最初から入れること。** 後付けにすると、動いている画面が CSP でまとめて壊れる。

このスキルの構成では CSP に次の許可が要る。ログイン画面が Google の
スクリプトを読み込むため、消すとログインボタンが出なくなる。

```
script-src  'self' https://accounts.google.com
frame-src   https://accounts.google.com
connect-src 'self' https://accounts.google.com
img-src     'self' data: https://*.googleusercontent.com   # プロフィール画像
```

`frame-ancestors 'none'` が入るので、**このアプリを他サイトの iframe に
埋め込めなくなる**。埋め込む必要が出たら、埋め込み元を明示的に列挙する。
`'none'` を消して全許可にしないこと。クリックジャッキングでボタンを
踏ませる攻撃が成立する。

### CORS

`allow_credentials=True` のとき、`allow_origins=["*"]` はブラウザが拒否する。
Origin は環境変数で列挙する。開発の都合で `*` にしたまま本番に出さない。

### オープンリダイレクト

ログイン後の戻り先に `?next=` を受けるなら、必ず検証する。
外部サイトに飛ばせると、フィッシングの踏み台になる。

```python
def _safe_login_redirect(request: Request) -> str:
    target = str(request.query_params.get("next") or "/")
    # "//evil.example.com" は「スキーム相対 URL」で外部に飛ぶ。/ 始まりだけでは足りない。
    return target if target.startswith("/") and not target.startswith("//") else "/"
```

### 入力検証

フロントの検証は入力補助であって防御ではない。**同じ検証をバックエンドにも書く。**
特に、パス・ファイル名として使う値は正規化する。

```python
# 悪い: ../.. で任意のファイルを読まれる
path = DATA_DIR / user_supplied_name

# 良い: 名前部分だけを取り、想定の文字集合に限定する
if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", user_supplied_name):
    raise ValueError("識別子の形式が不正です。")
path = DATA_DIR / user_supplied_name
```

### 秘密情報

- `.env` は `.gitignore`。`.env.sample` にはキー名だけを書き、値は空にする
- 本番は Secret Manager / k8s Secret から環境変数として渡す。`env.yaml` には入れない
- **ログ・エラーメッセージに秘密を出さない。** 例外をそのまま返すと、
  接続文字列や API キーが混ざることがある
- 秘密をコミットしてしまったら、履歴から消すだけでは足りない。**必ずローテートする**

## つまずきやすい点

| 症状 | 原因 |
|---|---|
| ログインボタンが出ない | `GOOGLE_OAUTH_CLIENT_ID` 未設定、または形式不正。CSP の `script-src` から `accounts.google.com` を消していないかも確認する |
| 押しても「origin ではない」と出る | Console の承認済み Origin とポート・ホスト名が不一致 |
| ログイン直後にまたログイン画面 | `APP_SESSION_SECRET` 未設定、または worker ごとに違う値 |
| 特定の人だけ 403 | `users.json` に未登録、`enabled: false`、または権限が付いていない |
| 開発時だけセッションが切れる | Vite(5173) から直接 8080 を叩いている。プロキシ経由にする |
| ログインを省いたのに 401 になる | `APP_ENV` が `local` 以外。`development` や `dev` では有効にならない |
| 本番で起動しない（`APP_ENV=local`） | `env.yaml` / `cos.env` に `APP_ENV=local` が残っている。消す |
| 本番でログイン状態が保たれない | HTTPS 終端の裏で `Secure` Cookie が落ちている。プロキシ設定を確認 |

## 参照ファイル

- `references/google-signin.md` — Google 側の設定、ID トークン検証、他の IdP への置き換え
- `references/rbac.md` — 権限モデルの設計、グループ、リソース単位の権限
- `references/audit-log.md` — 記録する項目、保管、Cloud Logging との併用
- `references/security-checklist.md` — リリース前に通す確認リスト
