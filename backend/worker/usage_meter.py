"""1回の生成で使ったトークンを、生成元を問わず足していく入れ物。

生成元はそれぞれ使用量を「最後に」返す作りで、途中で失敗すると、そこまでに
使ったぶんが返らずに消えていた（推論の途中で落ちた Claude など）。
使用量が分かった時点で charge() で足しておき、失敗したときはこの合計を記録する。
成功したときは、これまでどおり各生成元が返した値を使う（ここと二重に数えない）。

生成ごとの入れ物は contextvars で持つ。生成元の関数の引数を増やさずに済み、
同じ Pod で別の生成が走っても混ざらない。
"""
from __future__ import annotations

import contextvars

_current: contextvars.ContextVar[dict | None] = contextvars.ContextVar("koyorina_usage", default=None)


def start() -> dict:
    """この生成の入れ物を作る。返した辞書に合計が溜まる。"""
    spent: dict = {}
    _current.set(spent)
    return spent


def charge(usage) -> None:
    """使用量を足す。生成の外（入れ物が無いとき）や空の値なら何もしない。"""
    spent = _current.get()
    if spent is None or not isinstance(usage, dict):
        return
    for key, value in usage.items():
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            spent[key] = spent.get(key, 0) + value
