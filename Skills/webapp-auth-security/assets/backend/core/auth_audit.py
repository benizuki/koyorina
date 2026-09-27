"""監査ログ。

「誰が」「いつ」「何をしたか」を JSONL に追記する。
アプリケーションログ（標準出力）と分けているのは、保管期間と閲覧権限が
別物だから。監査ログは追跡のために生のメールアドレスを持つ代わりに、
閲覧を can_manage_users 権限に限定する。

ファイルはサイズでローテートする。放置すると読み出しが重くなり、
ディスクも食う。
"""
from __future__ import annotations

import json
import os
import threading
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_MAX_LOGS = 1000
# ファイルが読めない環境（権限、マウント忘れ）でも直近の分は見られるよう、
# メモリ上にも保持しておく。
_LOGS: deque[dict[str, Any]] = deque(maxlen=_MAX_LOGS)
_LOCK = threading.Lock()

_LOG_PATH = Path(
    os.getenv(
        "AUTH_AUDIT_LOG_PATH",
        Path(__file__).resolve().parents[1] / "data" / "auth_audit_logs.jsonl",
    )
)
_LOG_MAX_BYTES = int(os.getenv("AUTH_AUDIT_LOG_MAX_BYTES", str(5 * 1024 * 1024)))
_LOG_BACKUP_COUNT = int(os.getenv("AUTH_AUDIT_LOG_BACKUP_COUNT", "5"))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _rotated_path(index: int) -> Path:
    return _LOG_PATH.with_name(f"{_LOG_PATH.name}.{index}")


def _rotate_logs_if_needed() -> None:
    if _LOG_MAX_BYTES <= 0 or _LOG_BACKUP_COUNT <= 0:
        return
    if not _LOG_PATH.exists() or _LOG_PATH.stat().st_size < _LOG_MAX_BYTES:
        return

    oldest = _rotated_path(_LOG_BACKUP_COUNT)
    if oldest.exists():
        oldest.unlink()
    for index in range(_LOG_BACKUP_COUNT - 1, 0, -1):
        src = _rotated_path(index)
        if src.exists():
            src.replace(_rotated_path(index + 1))
    _LOG_PATH.replace(_rotated_path(1))


def _append_log(entry: dict[str, Any]) -> None:
    with _LOCK:
        _LOGS.appendleft(entry)
        try:
            _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            _rotate_logs_if_needed()
            with _LOG_PATH.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False, separators=(",", ":")))
                f.write("\n")
        except Exception:
            # 監査ログの永続化に失敗しても本処理は止めない。
            # ログが書けないことより、業務が止まるほうが利用者への影響が大きい。
            return


def record_auth_login(payload: dict[str, Any]) -> None:
    """ログインの成否を記録する。

    失敗は理由まで残す。「同じ IP から未登録メールで失敗し続けている」
    のようなパターンを後から見つけられるようにするため。
    """
    _append_log({
        "timestamp": _now_iso(),
        "category": "authentication",
        "event": "auth.login",
        "outcome": "success" if payload.get("result") == "success" else "failure",
        "email": payload.get("email"),
        "role": payload.get("role"),
        "status_code": payload.get("status_code"),
        "detail": payload.get("reason") or payload.get("message"),
        "source_ip": payload.get("remote_ip"),
        "user_agent": payload.get("user_agent"),
        "method": "POST",
        "path": payload.get("path") or "/auth/google",
    })


def record_user_operation(
    *,
    actor_email: str,
    event: str,
    target_email: str,
    outcome: str = "success",
    detail: str = "",
    method: str = "",
    path: str = "",
) -> None:
    """権限やマスタを変える操作を記録する。

    「いつ誰にこの権限が付いたのか」に答えられるようにするため、
    ユーザー追加・権限変更・削除では必ず呼ぶ。
    業務上の重要な操作（削除など）にも同じ関数を流用してよい。
    """
    _append_log({
        "timestamp": _now_iso(),
        "category": "authorization",
        "event": event,
        "outcome": outcome,
        "email": actor_email,
        "status_code": 200 if outcome == "success" else 400,
        "detail": detail or f"target={target_email}",
        "target_email": target_email,
        "method": method,
        "path": path,
    })


def list_auth_audit_logs(limit: int = 500) -> list[dict[str, Any]]:
    """新しい順に返す。閲覧は can_manage_users に限定すること。"""
    safe_limit = max(1, min(limit, _MAX_LOGS))
    try:
        entries: list[dict[str, Any]] = []
        paths = [_LOG_PATH] + [_rotated_path(i) for i in range(1, _LOG_BACKUP_COUNT + 1)]
        for path in paths:
            if not path.is_file():
                continue
            lines = path.read_text(encoding="utf-8").splitlines()
            # ファイル全体を読まず末尾側だけを見る。壊れた行は黙って飛ばす。
            for line in reversed(lines[-safe_limit * 2:]):
                if not line.strip():
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(item, dict):
                    entries.append(item)
                if len(entries) >= safe_limit:
                    return entries
        if entries:
            return entries
    except Exception:
        pass
    # ファイルが読めないときはメモリ上の分を返す。
    with _LOCK:
        return list(_LOGS)[:safe_limit]
