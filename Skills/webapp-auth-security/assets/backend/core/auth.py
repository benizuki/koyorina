"""Google ログインとロールベースのアクセス制御。

設計の要点:
- Google の署名検証を通っただけでは通さない。自前のユーザーマスタに
  登録済みかつ有効であることを必ず確認する（Google アカウントは誰でも持てる）。
- 判定はロール名ではなく機能キー（can_xxx）で行う。ロールは既定値のセットに過ぎない。
- セッションに OAuth クライアント ID の指紋を入れ、設定変更時に古いセッションを無効化する。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2 import id_token
from starlette.middleware.sessions import SessionMiddleware

from core.auth_audit import record_auth_login
from core.dev_auth import dev_auth_enabled, dev_permissions, dev_role, dev_user
from core.user_store import PermissionRecord, normalize_permissions, resolve_user

auth_router = APIRouter()
TEMPLATES_DIR = Path(__file__).resolve().parents[1] / "templates"

# 認証イベントは 1 行 1 JSON で標準出力に出す。Cloud Logging も
# k8s のログ基盤もこの形をそのまま構造化ログとして拾える。
auth_event_logger = logging.getLogger("app.auth")
if not auth_event_logger.handlers:
    _handler = logging.StreamHandler(sys.stdout)
    _handler.setFormatter(logging.Formatter("%(message)s"))
    auth_event_logger.addHandler(_handler)
auth_event_logger.setLevel(logging.INFO)
auth_event_logger.propagate = False

_GOOGLE_CLIENT_ID_RE = re.compile(r"^[0-9]+-[a-z0-9-]+\.apps\.googleusercontent\.com$")


def init_oauth(app) -> None:
    """セッションミドルウェアを登録する。認証ルータより先に呼ぶこと。

    APP_SESSION_SECRET は os.environ で読む。未設定なら起動時に落ちてよい。
    設定漏れのまま動いてしまうより、その場で気付けるほうが安全。

    開発時（APP_ENV=local）だけは固定値で代用する。
    セッションを使わないモードなので、ここで落ちる意味が無く、
    「まず動かす」までに必要な設定を減らせる。
    """
    if dev_auth_enabled():
        secret_key = os.getenv("APP_SESSION_SECRET") or "local-development-only-not-a-secret"
    else:
        secret_key = os.environ["APP_SESSION_SECRET"]
    app.add_middleware(SessionMiddleware, secret_key=secret_key)


# ── OAuth クライアント ID ──────────────────────────────

def google_oauth_client_id() -> str:
    return os.getenv("GOOGLE_OAUTH_CLIENT_ID", "").strip()


def _oauth_client_id_error(client_id: str | None = None) -> str:
    value = google_oauth_client_id() if client_id is None else client_id.strip()
    if not value:
        return "GOOGLE_OAUTH_CLIENT_ID が設定されていません。"
    if not _GOOGLE_CLIENT_ID_RE.fullmatch(value):
        return "GOOGLE_OAUTH_CLIENT_ID の形式が不正です。Google OAuth のウェブクライアント ID を設定してください。"
    return ""


def _oauth_client_id_fingerprint(client_id: str | None = None) -> str:
    value = google_oauth_client_id() if client_id is None else client_id.strip()
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


# ── セッション ────────────────────────────────────────

def _session_is_valid(request: Request) -> bool:
    """セッションが今の設定の下で有効かを判定する。

    クライアント ID の指紋を突き合わせているのは、OAuth 設定を差し替えたのに
    古い設定で発行されたセッションが生き続ける状態を防ぐため。
    """
    user = request.session.get("user")
    if not isinstance(user, dict) or not user.get("email"):
        return False
    configured_id = google_oauth_client_id()
    if _oauth_client_id_error(configured_id):
        return False
    session_fingerprint = str(request.session.get("oauth_client_id_fingerprint") or "")
    return bool(
        session_fingerprint
        and hmac.compare_digest(session_fingerprint, _oauth_client_id_fingerprint(configured_id))
    )


def current_user(request: Request) -> dict[str, Any] | None:
    # 開発時（APP_ENV=local）は Google を通さず固定のユーザーとして扱う。
    # 有効になる条件と安全弁は core/dev_auth.py を参照。
    if dev_auth_enabled():
        return dev_user()
    if not _session_is_valid(request):
        return None
    return request.session.get("user")


def current_role(request: Request) -> str:
    if dev_auth_enabled():
        return dev_role()
    return request.session.get("role", "general")


def current_permissions(request: Request) -> PermissionRecord:
    if dev_auth_enabled():
        return dev_permissions()
    role = current_role(request)
    payload = request.session.get("permissions")
    return normalize_permissions(payload if isinstance(payload, dict) else None, role=role)


def auth_mode() -> str:
    """フロントに返す認証モード。

    "dev-bypass" のとき、画面は常時バナーを出す。
    認証が効いていない状態のまま気付かずに使い続けることを防ぐため。
    """
    return "dev-bypass" if dev_auth_enabled() else "google"


# ── 認可 ──────────────────────────────────────────────

def require_login(request: Request) -> dict[str, Any]:
    """未ログインなら 401。全エンドポイントの先頭で呼ぶ。

    例外は /healthz だけ（プローブが 302 を受け取ると死活監視が壊れる）。
    """
    user = current_user(request)
    if not user:
        # 指紋不一致などでセッションが無効になった場合、残骸を消してから返す。
        if request.session.get("user"):
            request.session.clear()
        raise HTTPException(
            status_code=401,
            detail={"error": "認証が必要です", "login_url": "/login"},
        )
    return user


def require_permission(request: Request, key: str, *, label: str = "") -> None:
    """機能キーで権限を検査する。

    ロール名で分岐（if role == "admin"）せず機能キーで見るのは、
    「この人にだけ例外的に許可したい」が来たときにロールを増やさずに済むため。
    """
    permissions = current_permissions(request)
    if not permissions.get(key):
        raise HTTPException(
            status_code=403,
            detail={
                "error": f"この操作には{label or key}の権限が必要です。管理者に権限の付与を依頼してください。",
                "your_role": current_role(request),
            },
        )


def require_user_management(request: Request) -> None:
    require_permission(request, "can_manage_users", label="ユーザー管理")


# ── ログの補助 ────────────────────────────────────────

def _client_ip(request: Request) -> str:
    # リバースプロキシ配下では X-Forwarded-For の先頭が実クライアント。
    # 後続の要素は経路上のプロキシで、クライアントが偽装できるため見ない。
    forwarded_for = request.headers.get("x-forwarded-for", "")
    if forwarded_for:
        return forwarded_for.split(",", 1)[0].strip()
    return request.client.host if request.client else ""


def _user_id_hash(email: str) -> str | None:
    """アプリケーションログ用の識別子。生のメールを載せないための代替。"""
    normalized = str(email or "").strip().lower()
    if not normalized:
        return None
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _cloud_trace_id(request: Request) -> str | None:
    """Cloud Logging でリクエスト単位にログをまとめるためのトレース ID。"""
    trace_header = request.headers.get("x-cloud-trace-context", "")
    trace_id = trace_header.split("/", 1)[0].strip()
    project_id = os.getenv("GOOGLE_CLOUD_PROJECT") or os.getenv("GCP_PROJECT")
    if not trace_id or not project_id:
        return None
    return f"projects/{project_id}/traces/{trace_id}"


def _log_google_login(
    request: Request,
    *,
    result: str,
    status_code: int,
    email: str = "",
    role: str = "",
    failure_reason: str = "",
) -> None:
    severity = "INFO" if result == "success" else "WARNING"
    payload = {
        "severity": severity,
        "message": "Google login succeeded" if result == "success" else "Google login failed",
        "event": "auth.login",
        "provider": "google",
        "result": result,
        "status_code": status_code,
        "email": email or None,
        "user_id_hash": _user_id_hash(email),
        "role": role,
        "reason": failure_reason,
        "remote_ip": _client_ip(request),
        "user_agent": request.headers.get("user-agent", ""),
        "path": str(request.url.path),
    }
    trace = _cloud_trace_id(request)
    if trace:
        payload["logging.googleapis.com/trace"] = trace
    record_auth_login(payload)
    auth_event_logger.info(json.dumps(payload, ensure_ascii=False))


# ── ログイン画面 ──────────────────────────────────────

def _login_page_html(client_id: str, config_error: str = "") -> str:
    template = (TEMPLATES_DIR / "login.html").read_text(encoding="utf-8")
    # json.dumps を通すのは、値を JS の文字列リテラルとして安全に埋めるため。
    return (
        template
        .replace("__GOOGLE_CLIENT_ID__", json.dumps(client_id, ensure_ascii=False))
        .replace("__GOOGLE_OAUTH_CONFIG_ERROR__", json.dumps(config_error, ensure_ascii=False))
    )


def _safe_login_redirect(request: Request) -> str:
    """ログイン後の戻り先を検証する。

    "//evil.example.com" はスキーム相対 URL として外部に飛ぶため、
    "/" 始まりの確認だけでは足りない。オープンリダイレクトはフィッシングの踏み台になる。
    """
    target = str(request.query_params.get("next") or "/")
    return target if target.startswith("/") and not target.startswith("//") else "/"


@auth_router.get("/login")
async def login(request: Request):
    redirect_target = _safe_login_redirect(request)
    if current_user(request):
        return RedirectResponse(url=redirect_target, status_code=302)
    client_id = google_oauth_client_id()
    config_error = _oauth_client_id_error(client_id)
    page = _login_page_html(client_id if not config_error else "", config_error)
    return HTMLResponse(
        page.replace("__LOGIN_REDIRECT__", json.dumps(redirect_target, ensure_ascii=False))
    )


# ── ID トークン検証 ───────────────────────────────────

def _verify_google_credential(credential: str) -> dict[str, Any]:
    client_id = google_oauth_client_id()
    config_error = _oauth_client_id_error(client_id)
    if config_error:
        raise HTTPException(status_code=500, detail=config_error)
    if not credential.strip():
        raise HTTPException(status_code=400, detail="credential が必要です。")
    try:
        # 第 3 引数の client_id で aud を検証する。ここを省くと、
        # 別アプリ向けに発行されたトークンを受け入れてしまう。
        user_info = id_token.verify_oauth2_token(credential, GoogleAuthRequest(), client_id)
    except Exception as exc:
        raise HTTPException(status_code=401, detail="Google 認証トークンの検証に失敗しました。") from exc

    if user_info.get("iss") not in {"accounts.google.com", "https://accounts.google.com"}:
        raise HTTPException(status_code=401, detail="Google 認証トークンの issuer が不正です。")
    if not user_info.get("email_verified", False):
        # 未確認メールを許すと、他人のメールを騙るアカウントを通してしまう。
        raise HTTPException(status_code=403, detail="メールアドレスが未確認のためログインできません。")
    return user_info


@auth_router.post("/auth/google")
async def auth_google(request: Request):
    body = await request.body()
    if not body:
        _log_google_login(request, result="failure", status_code=400, failure_reason="empty_request_body")
        raise HTTPException(status_code=400, detail="リクエスト本文が必要です。")
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        _log_google_login(request, result="failure", status_code=400, failure_reason="invalid_json")
        raise HTTPException(status_code=400, detail=f"JSON が不正です: {exc.msg}") from exc
    if not isinstance(payload, dict):
        _log_google_login(request, result="failure", status_code=400, failure_reason="non_object_json")
        raise HTTPException(status_code=400, detail="JSON オブジェクトを送信してください。")

    credential = str(payload.get("credential") or "")
    if not credential.strip():
        _log_google_login(request, result="failure", status_code=400, failure_reason="missing_credential")
        raise HTTPException(status_code=400, detail="credential が必要です。")
    try:
        user_info = _verify_google_credential(credential)
    except HTTPException as exc:
        _log_google_login(request, result="failure", status_code=exc.status_code, failure_reason=str(exc.detail))
        raise

    email: str = user_info.get("email", "")
    display_name: str = user_info.get("name", email)

    # ここが認可の入口。Google の検証を通っただけでは通さない。
    record = resolve_user(email, display_name)
    if record is None:
        _log_google_login(
            request, result="failure", status_code=403, email=email,
            failure_reason="user_not_allowed",
        )
        raise HTTPException(status_code=403, detail=f"{email} はこのアプリの利用を許可されていません。")

    request.session["user"] = {
        "email": email,
        "name": display_name,
        "picture": user_info.get("picture", ""),
    }
    request.session["role"] = record["role"]
    request.session["permissions"] = record["permissions"]
    request.session["oauth_client_id_fingerprint"] = _oauth_client_id_fingerprint()
    _log_google_login(request, result="success", status_code=200, email=email, role=record["role"])
    return JSONResponse({
        "ok": True,
        "user": request.session["user"],
        "role": record["role"],
        "permissions": record["permissions"],
    })


@auth_router.get("/logout")
async def logout(request: Request):
    # 個別キーの削除は消し漏れる。丸ごと消す。
    request.session.clear()
    if dev_auth_enabled():
        # セッションを見ていないのでログアウトしても入り直せる。
        # /login に飛ばすとループするため、トップへ返す。
        return RedirectResponse(url="/", status_code=302)
    return RedirectResponse(url="/login", status_code=302)


def http_exception_to_response(exc: HTTPException):
    """エラーレスポンスの形を 1 つに揃える。main.py の例外ハンドラから呼ぶ。"""
    detail = exc.detail
    if isinstance(detail, dict):
        return JSONResponse(status_code=exc.status_code, content=detail)
    return JSONResponse(status_code=exc.status_code, content={"error": detail})
