"""テストの雛形。

要点は 2 つ。
1. domain 層の関数は TestClient を使わず直接呼ぶ。速く、書くのが億劫にならない。
2. 永続ファイルの保存先を必ず差し替える。実データを壊す事故を防ぐ。
"""
from __future__ import annotations

import pytest


# 例: domain/orders.py をテストする場合
#
# import domain.orders as orders
#
#
# @pytest.fixture(autouse=True)
# def isolated_store(tmp_path, monkeypatch):
#     """保存先を一時ディレクトリに向ける。
#
#     autouse=True にしているのは、差し替え忘れたテストが 1 つでもあると
#     data/ の実データが壊れるため。個別に指定する運用にはしない。
#     """
#     monkeypatch.setattr(orders, "_STORE_PATH", tmp_path / "orders.json")
#
#
# def test_create_order_rejects_empty_items():
#     with pytest.raises(ValueError, match="注文明細"):
#         orders.create_order([], actor_email="a@example.com")
#
#
# def test_create_order_assigns_id():
#     record = orders.create_order([{"sku": "A-1", "qty": 2}], actor_email="a@example.com")
#     assert record["id"]
#     assert orders.list_orders() == [record]


def test_placeholder():
    assert True
