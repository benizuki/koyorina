"""環境変数をここ 1 箇所に集約する。

os.getenv をアプリ各所に書くと、必要な設定の全体像が誰にも分からなくなり、
デプロイのたびに設定漏れで落ちる。新しい環境変数を足したら、
必ず .env.sample と env.yaml も同じ変更の中で更新すること。
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()
load_dotenv(Path(__file__).resolve().parents[1] / ".env")

_BACKEND_DIR = Path(__file__).resolve().parents[1]


def _env_flag(*keys: str, default: bool = False) -> bool:
    """真偽値の環境変数。複数キーを渡すと、先に見つかったものを使う。

    キー名をリネームしたいとき、移行期間中は新旧の両方を並べて受け付けられる。
    """
    for key in keys:
        value = os.getenv(key)
        if value is None:
            continue
        return value.lower() in {"1", "true", "yes", "on"}
    return default


def _env_str(*keys: str, default: str = "") -> str:
    for key in keys:
        value = os.getenv(key)
        if value is None:
            continue
        normalized = value.strip()
        if normalized:
            return normalized
    return default


def _env_int(*keys: str, default: int = 0) -> int:
    for key in keys:
        value = os.getenv(key)
        if value is None:
            continue
        try:
            return int(value.strip())
        except ValueError:
            continue
    return default


def _env_csv(*keys: str, default: tuple[str, ...] = ()) -> tuple[str, ...]:
    for key in keys:
        value = os.getenv(key)
        if value is None:
            continue
        items = tuple(item.strip() for item in value.split(",") if item.strip())
        if items:
            return items
    return default


class Settings:
    # 無いと動かない設定は os.environ で読む。起動時に落として気付かせるため。
    session_secret: str = os.environ["APP_SESSION_SECRET"]

    # 任意の設定は既定値付きで読む。
    google_oauth_client_id: str = _env_str("GOOGLE_OAUTH_CLIENT_ID")
    allowed_origins: tuple[str, ...] = _env_csv(
        "CORS_ALLOWED_ORIGINS",
        default=("http://127.0.0.1:8080",),
    )

    data_dir: Path = Path(_env_str("DATA_DIR", default=str(_BACKEND_DIR / "data")))

    # 機能フラグ。未完成の機能は既定 false で入れておくと、
    # 本番に出さずにマージできる。
    # feature_x_enabled: bool = _env_flag("FEATURE_X_ENABLED", default=False)


@lru_cache
def get_settings() -> Settings:
    """設定は 1 度だけ読む。プロセス起動後の環境変数変更は反映されない。"""
    return Settings()
