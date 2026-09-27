# 権限モデルの設計

## 目次

- [基本方針](#基本方針)
- [機能キーの決め方](#機能キーの決め方)
- [ロールは既定値のセット](#ロールは既定値のセット)
- [機能キーを増やすとき](#機能キーを増やすとき)
- [グループで権限を配る](#グループで権限を配る)
- [リソース単位の権限](#リソース単位の権限)
- [締め出しを防ぐ](#締め出しを防ぐ)

## 基本方針

**判定は機能キー（`can_xxx`）で行い、ロール名で分岐しない。**

```python
# 悪い: 「Bさんにだけレポート出力を許したい」が来た瞬間に破綻する
if current_role(request) != "admin":
    raise HTTPException(403, ...)

# 良い
require_permission(request, "can_export", label="出力")
```

ロール名で分岐すると、例外的な許可のたびに `power_with_export` のような
ロールが増えていく。最終的に誰も全体像を把握できなくなる。
機能キーなら、そのユーザーのキーを 1 つ真にするだけで済む。

## 機能キーの決め方

**画面の操作単位ではなく、守りたいデータや副作用の単位で決める。**
ボタンごとにキーを作ると数が爆発し、管理画面で設定できなくなる。

目安は 3〜7 個。それを超えたら、統合できないか見直す。

```python
class PermissionRecord(TypedDict):
    can_view:         bool   # 参照
    can_edit:         bool   # 登録・更新・削除
    can_export:       bool   # 外部に出せる形での出力
    can_manage_users: bool   # ユーザーと権限の管理
```

分ける価値があるのは、こういう軸。

- **参照と更新** — 「見せてよいが変えさせたくない」は必ず出てくる
- **外部に出せる操作** — ダウンロード、メール送信、外部連携。
  情報の持ち出しに直結するので、参照とは別に管理したい
- **管理操作** — 権限の付与自体。ここが一番強い
- **機微なデータ領域** — 人事情報、原価など、同じ操作でも対象で分けたいもの

逆に分けなくてよいもの: 一覧と詳細、作成と更新、画面ごとの違い。

`can_manage_users` は常に持たせておく。ユーザー管理画面と監査ログ閲覧が
そのまま流用でき、アプリごとに作り直さずに済む。

## ロールは既定値のセット

ロールは「機能キーの初期値の組み合わせ」に過ぎない。
ユーザー登録時の入力を減らすためのショートカットとして扱う。

```python
def _permissions_for_role(role: str) -> PermissionRecord:
    if role == "admin":
        return {"can_view": True, "can_edit": True, "can_export": True, "can_manage_users": True}
    if role == "power":
        return {"can_view": True, "can_edit": True, "can_export": True, "can_manage_users": False}
    return {"can_view": True, "can_edit": False, "can_export": False, "can_manage_users": False}
```

登録後に個別のキーを上書きできるようにしておく。
`normalize_permissions()` は「保存された値があればそれを使い、なければロール既定値」
という優先順で埋める。

## 機能キーを増やすとき

既存ユーザーのレコードには新しいキーが無い。
`normalize_permissions()` がロール既定値で埋めるので、データ移行は不要。

**ただし、既定値を「真」にすると、既存の全ユーザーがその機能を使えるようになる。**
新しい機能キーは、まず `general` で偽にしておき、必要な人に個別に付ける。
「気付かないうちに全員に権限が渡っていた」という事故を防げる。

## グループで権限を配る

人数が増えると、ユーザー 1 人ずつの設定が現実的でなくなる。
その段階でグループを導入する。

```python
class GroupRecord(TypedDict):
    id: str
    name: str
    members: list[str]      # メールアドレス
```

グループは 2 通りの使い方がある。

1. **権限のテンプレート** — グループに機能キーを持たせ、所属者に合成する
2. **リソースの閲覧範囲** — 「このレポートを見られるのはこのグループ」

社内ツールでは 2 の用途のほうが多い。1 はロールでほぼ足りる。

合成するときは **OR で合成する**（どれか 1 つでも真なら真）。
AND にすると、グループを増やすほど何もできなくなり、直感に反する。

## リソース単位の権限

「レポート A は営業部だけ、レポート B は製造部だけ」のような場合、
機能キーでは表現できない。リソース側に閲覧・操作できるグループを持たせる。

```python
class ReportDefinition(TypedDict):
    id: str
    name: str
    viewer_group_ids: list[str]     # 閲覧できるグループ
    operator_group_ids: list[str]   # 実行・編集できるグループ
```

```python
def can_view_report(email: str, report: ReportDefinition) -> bool:
    if not report["viewer_group_ids"]:
        return True                       # 未設定なら全員に見せる（運用開始時の混乱を避ける）
    return any(email in group["members"] for group in _groups(report["viewer_group_ids"]))
```

**一覧を返すエンドポイントでの絞り込みを忘れないこと。**
詳細取得だけを守って一覧を素通しにすると、名前や件数から中身が推測される。

```python
@router.get("/api/reports")
def api_reports(request: Request):
    user = require_login(request)
    # 見えないものは一覧にも出さない
    return {"reports": [r for r in list_reports() if can_view_report(user["email"], r)]}
```

## 締め出しを防ぐ

管理者が自分の管理権限を外す・自分を無効化する・自分を削除すると、
誰も設定を変えられなくなる。復旧には `users.json` の手編集が要る。

```python
if actor_email in existing["emails"]:
    if not new_permissions.get("can_manage_users", True):
        return _json_error("自分自身のユーザー管理権限は外せません。", 400)
    if not enabled:
        return _json_error("自分自身を無効化することはできません。", 400)
```

「管理者が 0 人になる操作を拒否する」という形でも書けるが、
自分自身への操作を止めるほうが単純で、意図も伝わりやすい。
