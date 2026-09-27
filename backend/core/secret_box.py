"""DBに置く秘密の暗号化。テナントのGemini APIキーなどに使う。

鍵は TENANT_SECRET_KEY（32文字以上の任意の文字列）。そのままではFernetの鍵の形では
ないので、SHA-256で32バイトにしてから使う。運用側は token_urlsafe(48) を置くだけでよい。
鍵が無い環境では暗号化できないので、呼び出し側は SecretBoxUnavailable を画面向けの
説明に変えて返す。平文で保存する逃げ道は作らない。
"""
from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken


class SecretBoxUnavailable(RuntimeError):
    """鍵が無い、または保存済みの値を今の鍵で開けない。"""


def _fernet(key: str) -> Fernet:
    if len(key or "") < 32:
        raise SecretBoxUnavailable("TENANT_SECRET_KEY が設定されていません。")
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(key.encode()).digest()))


def available(key: str) -> bool:
    return len(key or "") >= 32


def seal(key: str, value: str) -> str:
    return _fernet(key).encrypt(value.encode()).decode()


def open_(key: str, token: str) -> str:
    try:
        return _fernet(key).decrypt(token.encode()).decode()
    except InvalidToken:
        # 鍵を入れ替えた、または壊れた値。黙って空にせず、再登録が要ると伝える。
        raise SecretBoxUnavailable("保存済みの値を開けません。鍵が変わった可能性があります。再登録してください。") from None
