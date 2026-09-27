"""FastAPI エントリポイント。

ここではアプリの組み立てだけを行い、業務ロジックは api/ と domain/ に置く。
ミドルウェアとルータは登録順で挙動が変わるため、順番を崩さないこと。
"""
from __future__ import annotations

import logging
import os

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from starlette.middleware.cors import CORSMiddleware

from core.api_common import FRONTEND_DIST_DIR, _frontend_index, _json_error
from core.auth import auth_router, current_user, http_exception_to_response, init_oauth
from core.dev_auth import assert_safe_environment
from core.security_headers import SecurityHeadersMiddleware
from api.general_api import router as general_router


logger = logging.getLogger(__name__)

# APP_ENV=local（認証スキップ）のままクラウドで起動しようとしていないかを最初に確認する。
# 警告ログではなく例外にするのは、ログは見落とされるが起動失敗は必ず気付かれるため。
assert_safe_environment()

app = FastAPI(title="__APP_TITLE__", version="1.0.0")

# 1. セキュリティヘッダー。ミドルウェアは後から足したものほど外側に来るため、
#    最初に登録することで、他のミドルウェアが返すエラー応答にもヘッダーが付く。
#    最初から入れておくこと。後付けにすると、動いていた画面が CSP で
#    まとめて壊れ、原因の切り分けに時間を取られる。
app.add_middleware(SecurityHeadersMiddleware)

# 2. セッション。認証ルータより先に入れないと request.session が使えない。
init_oauth(app)

# 3. 認証（/login, /auth/google, /logout）
app.include_router(auth_router)

# 4. CORS。同一オリジン配信なら実質不要だが、開発時の Vite から叩くために必要。
#    allow_credentials=True のとき allow_origins に "*" は使えない（ブラウザが拒否する）。
_cors_allowed_origins = [
    origin.strip()
    for origin in os.getenv("CORS_ALLOWED_ORIGINS", "http://127.0.0.1:8080").split(",")
    if origin.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 5. 業務ルータ
app.include_router(general_router)


@app.exception_handler(HTTPException)
async def handle_http_exception(_: Request, exc: HTTPException):
    return http_exception_to_response(exc)


@app.exception_handler(Exception)
async def handle_exception(_: Request, exc: Exception):
    # 詳細はサーバログに残し、利用者にはスタックトレースを見せない。
    logger.exception("unexpected error")
    return JSONResponse(
        status_code=500,
        content={"error": f"{type(exc).__name__}: {exc}", "status_code": 500},
    )


@app.get("/")
def index(request: Request):
    if not current_user(request):
        return RedirectResponse(url="/login", status_code=302)
    return _frontend_index()


@app.get("/healthz")
def healthz():
    """死活監視用。認証を掛けないこと（プローブが 302 を受け取ってしまう）。"""
    return {"status": "ok"}


# 6. SPA フォールバック。全パスを受けるので、必ず最後に登録する。
@app.get("/{path:path}")
def spa_fallback(path: str):
    # API 相当のパスに index.html を返すと、フロント側は JSON パース失敗になり
    # 原因が追いにくい。ここで明示的に 404 を返す。
    if path.startswith(("api/", "auth/", "login", "logout", "healthz")):
        return _json_error("not found", 404)

    asset_path = FRONTEND_DIST_DIR / path
    if path and asset_path.is_file():
        return FileResponse(asset_path)
    return _frontend_index()


if __name__ == "__main__":
    import uvicorn

    logging.basicConfig(level=logging.INFO)
    uvicorn.run("main:app", host="0.0.0.0", port=8080, reload=False)
