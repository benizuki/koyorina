# Google サインインの設定と検証

## 目次

- [Google Cloud Console 側の設定](#google-cloud-console-側の設定)
- [ID トークンの検証で必ず見る項目](#id-トークンの検証で必ず見る項目)
- [Workspace ドメインで絞る](#workspace-ドメインで絞る)
- [他の IdP に置き換える](#他の-idp-に置き換える)
- [症状別の原因](#症状別の原因)

## Google Cloud Console 側の設定

1. 「APIとサービス」→「認証情報」→「認証情報を作成」→「OAuth クライアント ID」
2. アプリケーションの種類は **ウェブ アプリケーション**
3. **承認済みの JavaScript 生成元** に、ブラウザで実際に開く Origin を完全一致で登録

```
http://localhost:8080          ← ローカルで backend を直接開く場合
http://localhost:5173          ← Vite dev server を開く場合
https://myapp.example.com      ← 本番
```

ここが初回のつまずきの大半。Origin は「スキーム + ホスト + ポート」の完全一致で判定される。

- `localhost` と `127.0.0.1` は**別物**
- `:8080` と `:5173` も**別物**
- `http` と `https` も**別物**
- 末尾スラッシュは付けない

**承認済みのリダイレクト URI は不要。** Google Identity Services の
`ux_mode: 'popup'` はリダイレクトを使わず、JS のコールバックで ID トークンを受け取る。

同意画面（OAuth consent screen）は、社内利用なら「内部」を選ぶ。
これで Workspace 組織内のアカウントに限定でき、審査も不要になる。

## ID トークンの検証で必ず見る項目

`id_token.verify_oauth2_token(credential, GoogleAuthRequest(), client_id)` が
署名・有効期限・`aud`（クライアント ID）を検証する。**第 3 引数を省略しない。**
省くと `aud` の検証が行われず、別のアプリ向けに発行されたトークンを受け入れてしまう。

その上で、自分で確認するのは次の 3 つ。

```python
# 1. issuer
if user_info.get("iss") not in {"accounts.google.com", "https://accounts.google.com"}:
    raise HTTPException(401, "Google 認証トークンの issuer が不正です。")

# 2. メール確認済みか
#    未確認を許すと、他人のメールを騙るアカウントを通してしまう。
if not user_info.get("email_verified", False):
    raise HTTPException(403, "メールアドレスが未確認のためログインできません。")

# 3. このアプリの利用を許可された人か  ← ここが認可の入口
record = resolve_user(user_info["email"])
if record is None:
    raise HTTPException(403, "このアプリの利用を許可されていません。")
```

3 番目が最も重要。**Google アカウントは世界中の誰でも持てる。**
署名検証を通っただけで通してしまうと、URL を知っている全員が入れる。

## Workspace ドメインで絞る

自社ドメインのアカウントだけを通したいなら、`hd`（hosted domain）クレームを見る。

```python
allowed_domains = get_settings().allowed_google_domains   # ("example.co.jp",)
if allowed_domains and user_info.get("hd") not in allowed_domains:
    raise HTTPException(403, "許可されたドメインのアカウントでログインしてください。")
```

ただし `hd` はコンシューマの Gmail アカウントには付かない。
協力会社の Gmail を通す必要があるなら、ドメイン判定ではなく
ユーザーマスタでの個別登録に寄せる（既定の構成はこちら）。

**メールアドレスの文字列末尾で判定しない。** `evil-example.co.jp` のような
ドメインを取られると通ってしまう。`hd` クレームを見ること。

## 他の IdP に置き換える

Microsoft Entra ID（Azure AD）や Okta に替える場合、変えるのは
`_verify_google_credential()` の中身と `login.html` のボタンだけで済む。
セッション、ユーザーマスタ、権限、監査ログはそのまま使える。

検証で見る項目は IdP が変わっても同じ構造になる。

| 見る項目 | 意味 |
|---|---|
| 署名 | IdP の公開鍵（JWKS）で検証する。自前で実装せずライブラリを使う |
| `iss` | 想定した IdP・テナントか |
| `aud` | 自分のアプリ向けに発行されたトークンか |
| `exp` / `nbf` | 有効期限内か |
| メール確認済みフラグ | IdP ごとにクレーム名が違う |
| 自前のユーザーマスタ | **どの IdP でも必ず必要** |

## 症状別の原因

| 症状 | 原因 |
|---|---|
| ボタンが描画されない | `GOOGLE_OAUTH_CLIENT_ID` 未設定・形式不正。ログイン画面に理由が出る |
| `The given origin is not allowed` | Console の承認済み Origin と不一致。ポート・ホスト名・スキームを確認 |
| `popup_closed_by_user` | 利用者が閉じただけ。エラー扱いにしないほうがよい |
| 401 「検証に失敗しました」 | クライアント ID の不一致か、トークンの期限切れ。時刻ずれも疑う |
| 403 「許可されていません」 | `users.json` に未登録、または `enabled: false`。想定どおりの動作 |
| ログイン直後にまたログイン画面 | セッションが保持されていない。`APP_SESSION_SECRET` を確認 |
| 本番だけログインが保たれない | HTTPS 終端の裏で Cookie が落ちている。プロキシの設定を確認 |
