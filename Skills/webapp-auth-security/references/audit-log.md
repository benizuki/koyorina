# 監査ログ

## 何のために残すか

社内ツールの監査ログは、後から次の問いに答えるためにある。

- 「このデータを消したのは誰か」
- 「退職者のアカウントが使われていないか」
- 「この人にいつ権限が付いたのか」
- 「深夜に大量の出力をした人がいないか」

**問いに答えられない粒度なら、記録する意味は薄い。**
「操作しました」だけのログを大量に残すより、少数でも
「誰が・いつ・何に対して・結果はどうだったか」が揃っているほうがよい。

## 2 種類のログを分ける

| | アプリケーションログ | 監査ログ |
|---|---|---|
| 出力先 | 標準出力（Cloud Logging / k8s） | `data/*.jsonl`（アプリ内で閲覧） |
| 目的 | 障害調査 | 追跡・説明責任 |
| 個人情報 | 載せない（`user_id_hash` を使う） | メールアドレスを載せる |
| 閲覧 | 開発者・運用 | `can_manage_users` を持つ管理者のみ |
| 保管 | ログ基盤の既定 | サイズローテート（既定 5MB × 5 世代） |

分けている理由は、**保管期間と閲覧権限が違う**から。
アプリケーションログには開発者が広くアクセスするので、
そこに全員のメールアドレスが流れ続けるのは避けたい。

## 記録する項目

```json
{
  "timestamp": "2026-08-26T02:11:43.512+00:00",
  "category": "authentication",
  "event": "auth.login",
  "outcome": "failure",
  "email": "unknown@example.com",
  "role": "",
  "status_code": 403,
  "detail": "user_not_allowed",
  "source_ip": "203.0.113.10",
  "user_agent": "Mozilla/5.0 ...",
  "method": "POST",
  "path": "/auth/google"
}
```

| 項目 | なぜ要るか |
|---|---|
| `timestamp` | UTC の ISO 8601。ローカル時刻で書くと時差で並べ替えを間違える |
| `category` | `authentication` / `authorization` / アプリ固有。絞り込みの軸 |
| `event` | `auth.login`, `user.update` のようなドット区切り。前方一致で絞れる |
| `outcome` | `success` / `failure`。失敗だけを見る場面が多い |
| `email` | 誰が。追跡のためここだけは生の値を持つ |
| `detail` | 失敗理由や変更内容。**ここが一番効く** |
| `source_ip` | 社外からのアクセスの検知。プロキシ配下なら XFF の先頭 |
| `status_code` | HTTP の結果 |

## 失敗は理由まで書く

ログイン失敗を `failure` とだけ記録すると、後から何も分からない。
理由を区別して残すと、パターンが見える。

| `detail` | 意味 | 見えること |
|---|---|---|
| `user_not_allowed` | 未登録・無効化済み | 退職者や部外者のアクセス試行 |
| `email_not_verified` | メール未確認 | 不審なアカウント |
| `invalid_token` | 署名検証に失敗 | 設定不備、または攻撃 |
| `missing_credential` | 本文が不正 | 自動化ツールによる探索 |

「同じ IP から `user_not_allowed` が連続している」は、
ログイン画面が外部に露出していることを示す。IP 制限を検討する材料になる。

## 記録すべき操作

必ず記録する:

- ログインの成功・失敗
- ユーザーの追加・更新・削除、権限の変更
- データの削除
- 外部への出力（ダウンロード、送信、連携）
- 設定の変更

記録しない:

- 一覧・詳細の参照（量が多すぎて埋もれる）
- 進捗ポーリング

参照も追跡したいほど機微なデータを扱うなら、
監査ログではなくアクセスログ側で対応する（Cloud Logging に構造化ログで出す）。

## 呼び出し方

```python
record_user_operation(
    actor_email=actor["email"],
    event="order.delete",
    target_email=order_id,        # 対象の識別子。人でなくてもよい
    detail=f"items={len(order['items'])}",
    method="DELETE",
    path=f"/api/orders/{order_id}",
)
```

**記録の失敗で本処理を止めない。** `_append_log()` は例外を握りつぶしている。
ログが書けないことより、業務が止まるほうが利用者への影響が大きい。
ただし、法令上ログが必須の用途では逆の判断もありうる。その場合は明示的に変える。

## 保管とローテート

サイズでローテートする（既定 5MB × 5 世代 = 最大 25MB）。
`AUTH_AUDIT_LOG_MAX_BYTES` / `AUTH_AUDIT_LOG_BACKUP_COUNT` で変えられる。

**長期保管が必要なら、ファイルに頼らない。** ローテートで消える。
数か月以上残すなら、標準出力にも同じ内容を出して
Cloud Logging のシンクで BigQuery や GCS に流す。

```python
# 監査ログをアプリ内保存とログ基盤の両方に出す
record_auth_login(payload)                                  # data/*.jsonl
auth_event_logger.info(json.dumps(payload, ensure_ascii=False))  # 標準出力
```

Cloud Run で GCS を FUSE マウントしている場合、JSONL への追記は遅い。
書き込み頻度が高いなら、アプリ内保存をやめて Cloud Logging 一本にする。

## 閲覧画面

管理者向けに一覧を出す。フィルタは `category` と `outcome` があれば足りる。

```python
@router.get("/api/auth/audit-logs")
def api_auth_audit_logs(request: Request, limit: int = 500):
    require_login(request)
    require_user_management(request)   # 生のメールを含むので管理者限定
    return {"logs": list_auth_audit_logs(limit=limit)}
```

画面側は `v-data-table` に流し込み、`outcome === 'failure'` の行に色を付ける。
失敗だけを拾えることが、この画面の存在価値。
