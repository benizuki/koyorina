"""ユーザーマスタ。

data/users.json に一覧を持つ。レコードの形:

  {
    "email": "primary@example.com",
    "emails": ["primary@example.com", "alias@example.com"],
    "name": "表示名",
    "role": "admin" | "power" | "general",
    "permissions": { "can_view": true, "can_edit": false, ... },
    "enabled": true
  }

emails を配列で持っているのは、同じ人が複数のドメイン（社用と協力会社など）で
サインインしうるため。どれで入っても同じレコードに解決する。

機能キー（can_xxx）はアプリごとに書き換える。ロールは既定値のセットに過ぎず、
判定は必ず機能キーで行う。ロール名で分岐すると、例外的な許可が発生した瞬間に
ロールが増殖して管理不能になる。
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import TypedDict

_STORE_PATH = Path(__file__).resolve().parents[1] / "data" / "users.json"
# 1 プロセス内の同時書き込みを直列化する。
# 注意: worker やレプリカが複数になった時点でこのロックは無力になる。
# その規模になったら Firestore などの外部ストアに移すこと。
_lock = threading.Lock()

ROLES = {"admin", "power", "general"}


class PermissionRecord(TypedDict):
    """アプリに合わせて機能キーを書き換える。

    can_manage_users は共通で残しておくと、ユーザー管理画面をそのまま流用できる。
    """
    can_view:         bool
    can_edit:         bool
    can_export:       bool
    can_manage_users: bool


class UserRecord(TypedDict):
    email: str
    emails: list[str]
    name: str
    role: str
    permissions: PermissionRecord
    enabled: bool


def _normalize_email(value: str) -> str:
    return value.strip().lower()


def _permissions_for_role(role: str) -> PermissionRecord:
    """ロールごとの既定値。ここを書き換えてアプリに合わせる。"""
    if role == "admin":
        return {
            "can_view": True, "can_edit": True,
            "can_export": True, "can_manage_users": True,
        }
    if role == "power":
        return {
            "can_view": True, "can_edit": True,
            "can_export": True, "can_manage_users": False,
        }
    return {
        "can_view": True, "can_edit": False,
        "can_export": False, "can_manage_users": False,
    }


def normalize_permissions(data: dict | None, role: str = "general") -> PermissionRecord:
    """保存された権限を、現在の機能キー一覧に合わせて埋め直す。

    機能キーを増やしたとき、既存レコードにそのキーが無い。
    ここでロール既定値を当てることで、古いデータも壊れずに読める。
    未知のキーは落とすので、users.json に手で余計な項目を書いても影響しない。
    """
    base = _permissions_for_role(role)
    payload = data or {}
    return {key: bool(payload.get(key, base[key])) for key in base}  # type: ignore[return-value]


def _normalize_record(raw: dict) -> UserRecord:
    raw_role = str(raw.get("role") or "general").strip() or "general"
    role = raw_role if raw_role in ROLES else "general"
    email = _normalize_email(str(raw.get("email") or ""))

    emails_raw = raw.get("emails")
    emails = []
    if isinstance(emails_raw, list):
        emails = [_normalize_email(str(item)) for item in emails_raw if str(item).strip()]
    if email:
        emails.append(email)
    deduped_emails = sorted(set(emails))
    primary_email = email or (deduped_emails[0] if deduped_emails else "")

    return {
        "email": primary_email,
        "emails": deduped_emails or ([primary_email] if primary_email else []),
        "name": str(raw.get("name") or "").strip(),
        "role": role,
        "permissions": normalize_permissions(raw.get("permissions"), role=raw_role),
        "enabled": bool(raw.get("enabled", True)),
    }


def _load() -> list[UserRecord]:
    if not _STORE_PATH.exists():
        return []
    with open(_STORE_PATH, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        return []
    return [_normalize_record(item) for item in data if isinstance(item, dict)]


def _save(records: list[UserRecord]) -> None:
    _STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(_STORE_PATH, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)


def list_users() -> list[UserRecord]:
    with _lock:
        return _load()


def get_user(email: str) -> UserRecord | None:
    target = _normalize_email(email)
    with _lock:
        for record in _load():
            if target in record["emails"]:
                return record
    return None


def upsert_user(
    email: str,
    name: str,
    role: str,
    enabled: bool,
    *,
    emails: list[str] | None = None,
    permissions: dict | None = None,
) -> UserRecord:
    with _lock:
        records = _load()
        primary_email = _normalize_email(email)
        merged = [_normalize_email(item) for item in (emails or []) if str(item).strip()]
        merged.append(primary_email)
        merged_emails = sorted({item for item in merged if item})

        idx = next((i for i, u in enumerate(records) if primary_email in u["emails"]), None)
        record: UserRecord = {
            "email": primary_email,
            "emails": merged_emails,
            "name": name.strip(),
            "role": role.strip() if role.strip() in ROLES else "general",
            "permissions": normalize_permissions(permissions, role=role),
            "enabled": enabled,
        }
        if idx is None:
            records.append(record)
        else:
            records[idx] = record
        _save(records)
    return record


def delete_user(email: str) -> bool:
    target = _normalize_email(email)
    with _lock:
        records = _load()
        remaining = [u for u in records if target not in u["emails"]]
        if len(remaining) == len(records):
            return False
        _save(remaining)
    return True


def resolve_user(email: str, display_name: str = "") -> UserRecord | None:
    """ログイン時の認可判定。

    未登録なら None。無効化済みでも None を返す。ここで弾かないと、
    退職者のセッションが残っている間だけログインできてしまう。
    """
    _ = display_name
    record = get_user(email)
    if record is None or not record["enabled"]:
        return None
    return record
