"""ブラウザ向けセキュリティヘッダーを全レスポンスに付与する ASGI ミドルウェア。

アプリ側のコードに手を入れなくても、この 1 ファイルを入れておけば
XSS の被害範囲、クリックジャッキング、MIME スニッフィング、
リファラ経由の情報漏れを、まとめて抑えられる。

**新規アプリでは最初から入れること。** 後から入れると、
すでに動いている画面が CSP で壊れ、どの機能がどのディレクティブに
引っかかっているのかを一つずつ突き止める作業になる。
最初から入れておけば、壊れるのは「いま書いた 1 画面」だけで済む。
"""
from __future__ import annotations

import os
from collections.abc import Awaitable, Callable
from typing import Any

from starlette.datastructures import Headers, MutableHeaders

Send = Callable[[dict[str, Any]], Awaitable[None]]
Receive = Callable[[], Awaitable[dict[str, Any]]]
Scope = dict[str, Any]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]


def _extra_origins(env_key: str) -> list[str]:
    """CSP に足す追加オリジンを環境変数から読む。

    外部の API やフォントを呼ぶ機能を足したときに、コードを書き換えずに
    許可を足せるようにしてある。ただし常用しないこと。
    恒久的に必要なものは、下の定数に直接書いて履歴に残すほうがよい。
    """
    return [item.strip() for item in os.getenv(env_key, "").split(",") if item.strip()]


# Google ログイン（webapp-auth-security）を使う前提の許可リスト。
# ログインを付けないアプリなら accounts.google.com の行は消してよい。
#
# 外部サービスを呼ぶ機能を足したら、対応するディレクティブに追記する。
# 追記を忘れると、ブラウザのコンソールに CSP 違反として出る。画面は
# 「読み込み中のまま止まる」ように見えることが多いので、まずそこを疑う。
_SCRIPT_SRC = ["'self'", "https://accounts.google.com"]
_STYLE_SRC = ["'self'", "'unsafe-inline'", "https://accounts.google.com", "https://fonts.googleapis.com"]
_IMG_SRC = ["'self'", "data:", "blob:", "https://*.googleusercontent.com", "https://ssl.gstatic.com"]
_FONT_SRC = ["'self'", "data:", "https://fonts.gstatic.com"]
_CONNECT_SRC = ["'self'", "https://accounts.google.com"]
_FRAME_SRC = ["https://accounts.google.com"]


def build_content_security_policy() -> str:
    """CSP 文字列を組み立てる。

    'unsafe-inline' を style-src にだけ許しているのは、Vuetify が
    要素へ直接 style 属性を書き込むため。script-src には付けないこと。
    付けた瞬間に、CSP による XSS の緩和がほぼ意味を失う。
    """
    directives = {
        "default-src": ["'self'"],
        "base-uri": ["'self'"],
        # プラグインを一切読まない
        "object-src": ["'none'"],
        # 他サイトの iframe に埋め込ませない（クリックジャッキング対策）
        "frame-ancestors": ["'none'"],
        # form の送信先を自分自身に限る
        "form-action": ["'self'"],
        "script-src": _SCRIPT_SRC + _extra_origins("CSP_EXTRA_SCRIPT_SRC"),
        "style-src": _STYLE_SRC + _extra_origins("CSP_EXTRA_STYLE_SRC"),
        "img-src": _IMG_SRC + _extra_origins("CSP_EXTRA_IMG_SRC"),
        "font-src": _FONT_SRC,
        "connect-src": _CONNECT_SRC + _extra_origins("CSP_EXTRA_CONNECT_SRC"),
        "frame-src": _FRAME_SRC,
        "media-src": ["'self'", "blob:"],
        "worker-src": ["'self'", "blob:"],
        "manifest-src": ["'self'"],
    }
    return "; ".join(f"{name} {' '.join(values)}" for name, values in directives.items())


SECURITY_HEADERS: dict[str, str] = {
    "Content-Security-Policy": build_content_security_policy(),
    # 宣言と違う Content-Type で解釈させない。
    # これが無いと、アップロードされたテキストが HTML として実行されうる
    "X-Content-Type-Options": "nosniff",
    # frame-ancestors を解さない古いブラウザ向けの保険
    "X-Frame-Options": "DENY",
    # 外部サイトへ遷移するとき、パスやクエリを渡さない
    "Referrer-Policy": "strict-origin-when-cross-origin",
    # 使わない端末機能を明示的に切る。
    # マイク・位置情報を使う機能があるなら、その項目だけ (self) にする
    "Permissions-Policy": (
        "accelerometer=(), camera=(), display-capture=(), geolocation=(), "
        "gyroscope=(), magnetometer=(), microphone=(), payment=(), usb=()"
    ),
    "X-Permitted-Cross-Domain-Policies": "none",
    # 旧 XSS フィルタは、有効にするとかえって脆弱になる既知の問題がある。
    # 0 で明示的に無効化するのが現在の推奨
    "X-XSS-Protection": "0",
}


def is_https_request(scope: Scope) -> bool:
    """HTTPS で受けたリクエストか。

    Cloud Run や k3s の Ingress は TLS を手前で終端するので、
    アプリから見た scheme は http になる。X-Forwarded-Proto も見る必要がある。
    """
    if scope.get("scheme") == "https":
        return True
    forwarded = Headers(scope=scope).get("x-forwarded-proto", "")
    return forwarded.split(",", 1)[0].strip().lower() == "https"


class SecurityHeadersMiddleware:
    """全 HTTP レスポンスにセキュリティヘッダーとキャッシュ制御を付ける。"""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: dict[str, Any]) -> None:
            if message.get("type") == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in SECURITY_HEADERS.items():
                    headers[name] = value

                # HSTS は HTTPS で配信しているときだけ付ける。
                # http の開発環境で付けると、ブラウザがそのホストを
                # https に固定してしまい、localhost が開けなくなる
                if is_https_request(scope):
                    headers["Strict-Transport-Security"] = "max-age=31536000"

                path = str(scope.get("path", ""))
                if path == "/api" or path.startswith("/api/"):
                    # 権限を変えた直後に古い応答が返る、を防ぐ
                    headers["Cache-Control"] = "no-store, max-age=0"
                    headers["Pragma"] = "no-cache"
                    headers["Expires"] = "0"
                elif path.startswith("/assets/") and message.get("status") == 200:
                    # Vite の出力はファイル名にハッシュが入るので、長く持たせてよい
                    headers["Cache-Control"] = "public, max-age=31536000, immutable"
                elif headers.get("content-type", "").lower().startswith("text/html"):
                    # index.html は毎回確認させる。ここをキャッシュさせると、
                    # デプロイしても利用者に古い SPA が出続ける
                    headers["Cache-Control"] = "no-cache, max-age=0, must-revalidate"

            await send(message)

        await self.app(scope, receive, send_with_headers)
