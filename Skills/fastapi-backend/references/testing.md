# テスト

## 方針

**domain 層のテストを厚く、API 層のテストは薄く。**
`domain/` の関数はただの Python 関数なので、起動もモックも要らず一瞬で回る。
テストが速いと実際に書かれる。`TestClient` を使うテストは、
認証やルーティングの結線を確認したいときだけに絞る。

## セットアップ

```bash
cd backend
pip install pytest
pytest
```

`pytest.ini` に `pythonpath = .` を書いてあるので、`backend/` を作業ディレクトリに
すれば `import core.auth` のような書き方がそのまま通る。
仮想環境を切っていない環境もあるので、`python -m pytest` でも動くようにしておく。

## 永続ファイルを汚さない

これがこの構成でいちばん事故りやすい箇所。
ファイル保存を使う以上、テストが実データを書き換える危険が常にある。

```python
import pytest
import domain.orders as orders


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    """保存先を一時ディレクトリに向ける。

    autouse=True にしているのは、差し替え忘れたテストが 1 つでもあれば
    data/ の実データが壊れるため。個別指定の運用にはしない。
    """
    monkeypatch.setattr(orders, "_STORE_PATH", tmp_path / "orders.json")
```

保存先がモジュール定数ではなく `get_settings().data_dir` から来る設計なら、
`monkeypatch.setenv("DATA_DIR", str(tmp_path))` した上で
`get_settings.cache_clear()` を呼ぶ（`lru_cache` を張っているため）。

```python
@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
```

## domain 層のテスト

正常系より、**壊れた入力を弾けているか**を優先して書く。
利用者は想定外の値を入れてくるし、そこが落ちると原因が分かりにくい。

```python
def test_create_order_rejects_empty_items():
    with pytest.raises(ValueError, match="注文明細"):
        orders.create_order([], actor_email="a@example.com")


def test_create_order_rejects_non_list_items():
    with pytest.raises(ValueError):
        orders.create_order("A-1", actor_email="a@example.com")  # 文字列を渡された


def test_normalize_keeps_unknown_fields_out():
    record = orders._normalize({"id": "x", "items": [], "__proto__": "evil"})
    assert "__proto__" not in record
```

## 認証付きエンドポイントのテスト

`require_login` を依存性で差し替えるのがいちばん簡単。

```python
from fastapi.testclient import TestClient
import core.auth as auth
import main


@pytest.fixture
def client(monkeypatch):
    fake_user = {"email": "tester@example.com", "name": "Tester", "picture": ""}
    monkeypatch.setattr(auth, "current_user", lambda request: fake_user)
    monkeypatch.setattr(auth, "require_login", lambda request: fake_user)
    monkeypatch.setattr(auth, "current_role", lambda request: "admin")
    monkeypatch.setattr(
        auth, "current_permissions",
        lambda request: {"can_view": True, "can_edit": True, "can_manage_users": True},
    )
    return TestClient(main.app)


def test_me_returns_permissions(client):
    res = client.get("/api/me")
    assert res.status_code == 200
    assert res.json()["permissions"]["can_manage_users"] is True
```

**注意**: `api/xxx_api.py` が `from core.auth import require_login` で取り込んでいる場合、
`core.auth` 側を差し替えても、すでに束縛済みの名前は入れ替わらない。
その場合はルータのモジュールを対象に `monkeypatch.setattr(orders_api, "require_login", ...)`
とするか、最初から `import core.auth as auth` して `auth.require_login(...)` と呼ぶ形にしておく。

## 権限のテストは必ず書く

「権限がない人が叩いたら 403 になる」は、UI では確認できない
（ボタンを隠しているため）。ここが抜けると、URL を直接叩かれたときに素通りする。

```python
def test_orders_create_requires_edit_permission(client_without_edit):
    res = client_without_edit.post("/api/orders", json={"items": [{"sku": "A", "qty": 1}]})
    assert res.status_code == 403
```

## テストを追加すべきタイミング

- 権限チェックを追加・変更したとき（必須）
- 入力の正規化・検証ロジックを書いたとき（境界値を 2〜3 個）
- バグを直したとき（同じバグの再発を止める 1 本）

網羅率を目標にしない。上の 3 つを守るだけで、実運用で効く範囲は押さえられる。
