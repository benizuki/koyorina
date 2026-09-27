# API 実装レシピ

## 目次

- [ルータの追加手順](#ルータの追加手順)
- [入力の検証](#入力の検証)
- [長時間処理と進捗](#長時間処理と進捗)
- [外部 API / GCP クライアントの扱い](#外部-api--gcp-クライアントの扱い)
- [JSONL のローテート付き追記](#jsonl-のローテート付き追記)
- [Webhook 通知](#webhook-通知)
- [アクセスログのノイズ抑制](#アクセスログのノイズ抑制)

## ルータの追加手順

1 機能 1 ファイル。`api/<feature>_api.py` を作り、`main.py` で登録する。

```python
# api/orders_api.py
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

import domain.orders as orders
from core.api_common import _json_error, _request_json
from core.auth import require_login, require_permission
from core.auth_audit import record_user_operation

router = APIRouter()


@router.get("/api/orders")
def api_orders_list(request: Request):
    require_login(request)
    require_permission(request, "can_view")
    return {"orders": orders.list_orders()}


@router.post("/api/orders")
async def api_orders_create(request: Request):
    actor = require_login(request)
    require_permission(request, "can_edit")
    payload = await _request_json(request)
    try:
        record = orders.create_order(payload.get("items"), actor_email=actor["email"])
    except ValueError as exc:
        # domain は ValueError を投げる。HTTP のステータスは api 側で決める。
        return _json_error(str(exc), 400)
    # 変更操作は監査ログに残す。誰が何をしたかを後から追えるようにするため。
    record_user_operation(
        actor_email=actor["email"],
        event="order.create",
        target_email=record["id"],
        method="POST",
        path="/api/orders",
    )
    return JSONResponse(status_code=201, content=record)
```

`main.py` 側:

```python
from api.orders_api import router as orders_router
app.include_router(orders_router)   # SPA フォールバックより前に置く
```

パスは `/api/` から始める。`main.py` の SPA フォールバックが
`api/` で始まるパスを 404 にするので、この規約から外れると
404 の代わりに index.html が返り、原因が分かりにくいバグになる。

## 入力の検証

### 入力の形が固まっているとき — Pydantic

```python
from pydantic import BaseModel, Field

class OrderItem(BaseModel):
    sku: str = Field(min_length=1)
    qty: int = Field(gt=0)

class CreateOrderRequest(BaseModel):
    items: list[OrderItem] = Field(min_length=1)

@router.post("/api/orders")
def api_orders_create(body: CreateOrderRequest, request: Request):
    ...
```

FastAPI が 422 を返してくれるが、**メッセージは英語で、利用者には読めない**。
利用者向けの画面から叩く API なら、バリデーションエラーハンドラで日本語に変換するか、
`_request_json` + 手書き検証にする。

```python
from fastapi.exceptions import RequestValidationError

@app.exception_handler(RequestValidationError)
async def handle_validation_error(_: Request, exc: RequestValidationError):
    first = exc.errors()[0] if exc.errors() else {}
    field = ".".join(str(p) for p in first.get("loc", ())[1:]) or "入力値"
    return JSONResponse(
        status_code=400,
        content={"error": f"{field} の値が不正です。入力内容を確認してください。", "status_code": 400},
    )
```

### 部分更新を受けるとき — `_request_json` + 手書き

「指定されなかった項目は変えない」を表現したい場合、Pydantic のモデルでは
「未指定」と「null 指定」の区別が煩雑になる。`_request_json` で dict を受け、
`domain` 側で `ValueError` を投げる形が単純で済む。

```python
def upsert_group(group_id: str | None, name, members) -> GroupRecord:
    normalized_name = str(name or "").strip()
    if not normalized_name:
        raise ValueError("グループ名を入力してください。")
    if not isinstance(members, list):
        raise ValueError("メンバーは配列で指定してください。")
    ...
```

## 長時間処理と進捗

数十秒かかる処理は、HTTP を開いたまま待たない。
フロントが生成した `request_id` を受け取り、別エンドポイントで進捗を返す。

```python
# core/progress.py
import threading, time
from typing import Any

_PROGRESS: dict[str, dict[str, Any]] = {}
_LOCK = threading.Lock()
_TTL_SECONDS = 3600


def update_progress(request_id: str, **fields: Any) -> None:
    if not request_id:
        return
    now_ms = int(time.time() * 1000)
    with _LOCK:
        entry = _PROGRESS.setdefault(
            request_id, {"request_id": request_id, "started_at_ms": now_ms}
        )
        entry.update(fields)
        entry["updated_at_ms"] = now_ms
        entry["elapsed_ms"] = now_ms - entry["started_at_ms"]
        _expire_locked(now_ms)


def get_progress(request_id: str) -> dict[str, Any] | None:
    with _LOCK:
        return dict(_PROGRESS.get(request_id, {})) or None


def _expire_locked(now_ms: int) -> None:
    # 放置すると増え続けるので、古いものを落とす。
    cutoff = now_ms - _TTL_SECONDS * 1000
    for key in [k for k, v in _PROGRESS.items() if v.get("updated_at_ms", 0) < cutoff]:
        del _PROGRESS[key]
```

```python
@router.get("/api/progress/{request_id}")
def api_progress(request_id: str, request: Request):
    require_login(request)
    # まだ本処理が始まっていない可能性があるので、404 ではなく pending を返す。
    # 404 を返すとフロント側がエラー扱いして、ポーリングを止めてしまう。
    return get_progress(request_id) or {
        "request_id": request_id,
        "status": "pending",
        "message": "処理の開始を待っています",
        "elapsed_ms": 0,
    }
```

**この方式はプロセス内メモリなので、worker が複数あると別 worker の進捗が見えない。**
worker を増やすなら Redis などの共有ストアに移すか、`--workers 1` にする。

`message` には日本語で「今どの段階か」を入れる。
パーセンテージだけだと、止まっているのか進んでいるのか利用者に分からない。

## 外部 API / GCP クライアントの扱い

**クライアントはモジュールレベルで 1 回だけ作る。** リクエストごとに作ると、
認証のやり取りが毎回走って遅くなる。

```python
from functools import lru_cache
from google.cloud import bigquery

@lru_cache
def _client() -> bigquery.Client:
    return bigquery.Client(project=get_settings().project)
```

同期クライアントを `async def` の中で直接呼ばない。イベントループが止まる。

```python
# 悪い
@router.get("/api/rows")
async def api_rows():
    return _client().query(sql).result()   # 全リクエストが待たされる

# 良い（どちらでもよい）
@router.get("/api/rows")
def api_rows():                            # def なら FastAPI がスレッドに逃がす
    return _client().query(sql).result()

@router.get("/api/rows")
async def api_rows():
    return await asyncio.to_thread(_run_query, sql)
```

外部 API 呼び出しには必ずタイムアウトを付ける。付け忘れると、
相手が応答しないときにワーカーが埋まり、アプリ全体が止まる。

## JSONL のローテート付き追記

追記され続ける記録は、サイズでローテートする。

```python
_LOG_MAX_BYTES = int(os.getenv("APP_LOG_MAX_BYTES", str(5 * 1024 * 1024)))
_LOG_BACKUP_COUNT = int(os.getenv("APP_LOG_BACKUP_COUNT", "5"))


def _rotate_if_needed(path: Path) -> None:
    if _LOG_MAX_BYTES <= 0 or _LOG_BACKUP_COUNT <= 0:
        return
    if not path.exists() or path.stat().st_size < _LOG_MAX_BYTES:
        return
    oldest = path.with_name(f"{path.name}.{_LOG_BACKUP_COUNT}")
    if oldest.exists():
        oldest.unlink()
    for index in range(_LOG_BACKUP_COUNT - 1, 0, -1):
        src = path.with_name(f"{path.name}.{index}")
        if src.exists():
            src.replace(path.with_name(f"{path.name}.{index + 1}"))
    path.replace(path.with_name(f"{path.name}.1"))


def append_record(path: Path, entry: dict) -> None:
    with _LOCK:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            _rotate_if_needed(path)
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False, separators=(",", ":")))
                f.write("\n")
        except Exception:
            # 記録の失敗で本処理を止めない。ログが書けないことより、
            # 業務が止まることのほうが利用者への影響が大きい。
            return
```

## Webhook 通知

Slack や Teams への通知を入れるときは、**失敗しても本処理を止めない**。

```python
def notify(webhook_url: str, text: str) -> None:
    if not webhook_url:
        return
    try:
        httpx.post(webhook_url, json={"text": text}, timeout=10.0)
    except Exception as exc:
        _logger.warning("webhook notification failed: %s", exc)
```

Webhook URL は秘密情報として扱う。`env.yaml` ではなく Secret 経由で渡す。

## アクセスログのノイズ抑制

進捗ポーリングのような高頻度エンドポイントは、アクセスログから外す。
放っておくとログの大半がこれになり、本物のエラーが埋もれる。

```python
class _ProgressAccessLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        return " /api/progress/" not in message


for logger_name in ("uvicorn.access", "gunicorn.access"):
    logging.getLogger(logger_name).addFilter(_ProgressAccessLogFilter())
```
