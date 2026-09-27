"""Podログの出し方。アプリ自身のログと、API呼び出しの1行ログ。

uvicorn が設定するのは uvicorn.* のロガーだけで、koyorina.* には出力先が無い。
そのままだと Python の既定で WARNING 以上しか出ず、INFO は黙って捨てられる
（codex-controller のPodログが空だったのはこのため）。
"""
from __future__ import annotations

import logging
import re
import sys
import time

access_logger = logging.getLogger("koyorina.access")

# 数秒おきに画面から呼ばれる状態確認。毎回出すとログが埋まり、ほかの行が読めなくなる。
# 失敗したときと遅いときだけ出す。
POLLING = re.compile(
    r"(?:/jobs/[0-9a-f-]{36}(?:/progress)?"   # 生成の状態・進捗
    r"|/runtimes?|/account"                   # 実行環境・Codex接続の状態
    r"|/projects/[0-9a-f-]{36}/interview"     # ヒアリングの状態
    r"|/tenants/[0-9a-f-]{36}/projects/[0-9a-f-]{36}(?:/logs)?"  # プレビューの状態・ログ
    r")$")
SILENT = {"/healthz"}
SLOW_MS = 2000


def configure_logging() -> None:
    """koyorina.* を INFO で標準出力へ出す。何度呼んでも1つだけ付ける。

    上位（root）への伝播は残す。テストの caplog は root で受け取るため。
    本番の root には出力先が無いので、同じ行が二重に出ることはない。
    """
    logger = logging.getLogger("koyorina")
    if any(getattr(handler, "koyorina", False) for handler in logger.handlers):
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.koyorina = True
    # uvicorn の既定の行と見た目を揃える。
    handler.setFormatter(logging.Formatter("%(levelname)s:     %(name)s %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


def should_log(method: str, path: str, status: int, elapsed_ms: float) -> bool:
    if path in SILENT:
        return False
    if method == "GET" and POLLING.search(path):
        return status >= 400 or elapsed_ms >= SLOW_MS
    return True


class RequestLogMiddleware:
    """API呼び出しを1行ずつ出す。メソッド・パス・ステータス・処理時間だけ。

    クエリ文字列（ファイル閲覧の ?path= など）、ヘッダー、本文は出さない。
    パスの ID はそのまま出す。どのテナント・利用者・ジョブの呼び出しかを追えないと、
    ログの意味が無い。
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        started = time.perf_counter()
        status = 500

        async def capture(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, capture)
        finally:
            elapsed = (time.perf_counter() - started) * 1000
            method, path = scope.get("method", ""), scope.get("path", "")
            if should_log(method, path, status, elapsed):
                level = logging.WARNING if status >= 500 else logging.INFO
                access_logger.log(level, "%s %s %d %dms", method, path, status, elapsed)
