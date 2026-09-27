"""アプリ全体で使う汎用エンドポイント。"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Request

from core.auth import auth_mode, current_permissions, current_role, require_login


router = APIRouter()
_logger = logging.getLogger(__name__)


@router.get("/api/me")
def api_me(request: Request):
    """フロントが起動時に呼ぶ。ログイン状態と権限をまとめて返す。

    フロントはこの応答を待ってから画面を描く。先に描くと、
    権限のないボタンが一瞬見えてしまう。
    """
    user = require_login(request)
    return {
        "user": user,
        "role": current_role(request),
        "permissions": current_permissions(request),
        # "dev-bypass" のとき、画面は常時バナーを出す。
        # 認証が効いていないことに気付かないまま使い続けるのを防ぐ。
        "auth_mode": auth_mode(),
    }
