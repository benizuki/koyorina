from uuid import UUID
from fastapi import APIRouter, HTTPException, Request
from starlette.concurrency import run_in_threadpool
from backend.core.auth import actor, audit
from backend.core.db import Tenant
from backend.domain.roles import can_develop_in
from backend.core.generation_client import controller
from backend.api.tenant_llm import generation_settings
from backend.core import gemini_client
from backend.worker.controller import antigravity_configured
from backend.domain.generation import gemini_options, openai_compatible_options

router = APIRouter(prefix="/api/codex")


def access(request: Request) -> tuple[str, bool]:
    with request.app.state.sessions() as db:
        user = actor(request, db)

        return user.id, request.app.state.settings.codex_enabled and user.codex_enabled


def runtime_access(request: Request, tenant_id: UUID) -> str:
    with request.app.state.sessions() as db:
        user = actor(request, db)

        # 生成環境はアプリを作る人のためのもの。使うだけの人のぶんまで起こさない。
        tenant = db.get(Tenant, str(tenant_id))

        if tenant is None or not tenant.enabled or not can_develop_in(db, user, tenant_id):
            raise HTTPException(403, "このテナントの生成環境は利用できません。")

        return user.id


@router.get("/status")
async def status(request: Request):
    user_id, allowed = await run_in_threadpool(access, request)

    if not allowed:
        return {"status": "unavailable", "email": None, "plan": None, "login": None,
                "error": None, "busy": False, "codex_allowed": False}

    if not request.app.state.settings.codex_controller_url:
        return {"status": "unavailable", "email": None, "plan": None, "login": None, "error": None, "busy": False}

    return await controller(request.app.state.settings, user_id, "GET", "/account")


@router.get("/runtimes")
async def runtimes(request: Request, tenant_id: UUID | None = None):
    """Current user's Codex/Gemini generation Pod state for the application bar."""
    user_id, codex_allowed = await run_in_threadpool(access, request)

    # テナントを渡されたら、そのテナントの生成AIの設定（無ければシステムの既定）で出す。
    settings = await run_in_threadpool(generation_settings, request, tenant_id)
    empty = {name: {"state": "stopped", "pods": 0, "running": 0,
                    "starting": 0, "errors": 0}
             for name in ("codex", "gemini", "antigravity", "openai_compatible", "claude")}

    # 生成controllerはCodex専用ではない。Gemini等の生成も同じワーカーで動くので、
    # Codexを全体で止めていても状態は聞く（聞かないと、動いていても「停止」に見える）。
    if settings.codex_controller_url:
        empty = await controller(settings, user_id, "GET", "/runtimes")

    if not codex_allowed:
        empty["codex"] = {**empty["codex"], "state": "unavailable"}

    if not gemini_client.available_in(settings):
        empty["gemini"] = {**empty["gemini"], "state": "unavailable"}

    if not antigravity_configured(settings):
        empty["antigravity"] = {**empty["antigravity"], "state": "unavailable"}

    if not getattr(settings, "openai_compatible_enabled", False):
        empty["openai_compatible"] = {**empty["openai_compatible"], "state": "unavailable"}

    empty.setdefault("claude", {"state": "stopped", "pods": 0, "running": 0, "starting": 0, "errors": 0})

    if not getattr(settings, "claude_enabled", False):
        empty["claude"] = {**empty["claude"], "state": "unavailable"}

    return empty


@router.post("/runtimes/{tenant_id}/start", status_code=202)
async def start_runtime(tenant_id: UUID, request: Request):
    """Warm the selected tenant's shared worker immediately after Koyorina login."""
    user_id = await run_in_threadpool(runtime_access, request, tenant_id)

    return await controller(request.app.state.settings, user_id, "POST", "/runtime/start",
                            tenant_id=tenant_id)


def claude_options(model: str, where: str) -> list[dict]:
    """Claude の選択肢。設定したモデルを既定にし、もう一方の代表的なモデルも並べる。"""
    names = [model] + [name for name in ("claude-sonnet-5", "claude-opus-5-5") if name != model]

    return [{"id": name, "label": f"{name}（Claude・{where}）", "provider": "claude",
             "description": "Claude Agent SDK で生成します。コマンドは実行しません。",
             "efforts": ["low", "medium", "high", "xhigh", "max"], "default_effort": "high",
             "is_default": False} for name in names if name]


@router.get("/models")
async def models(request: Request, tenant_id: UUID | None = None):
    """選べるモデル。Codexは本人の接続から、Gemini・Antigravity・OpenAI互換は運用設定から出す。

    テナントを渡されたら、そのテナントの生成AIの設定（無ければシステムの既定）で出す。
    """
    settings = await run_in_threadpool(generation_settings, request, tenant_id)
    user_id, allowed = await run_in_threadpool(access, request)
    options = []

    if allowed and settings.codex_enabled and settings.codex_controller_url:
        try:
            options += (await controller(settings, user_id, "GET", "/models")).get("models", [])
        except HTTPException:
            pass  # Codex未接続でもGeminiは選べる。

    if gemini_client.available_in(settings):
        gemini = gemini_options(settings.gemini_models)
        backend_label = ("Vertex AI" if settings.gemini_api_backend == "vertex"
                         else "Google AI API")
        for option in gemini:
            option["label"] = f"{option['label']}（{backend_label}）"
        options += gemini

    if antigravity_configured(settings):
        # UIの選択値はモデルIDだけでなく経路も識別できる必要がある。
        # Geminiと同じモデル名を返すと、Antigravityを選んでもGeminiへ送られる。
        options.append({"id": f"antigravity-{settings.antigravity_model}",
                        "label": f"{settings.antigravity_model}（Antigravity）", "provider": "antigravity",
                        "description": "Google管理のRemote Sandboxで生成します。",
                        "efforts": ["medium"],
                        "default_effort": "medium", "is_default": False})

    if getattr(settings, "openai_compatible_enabled", False):
        options += openai_compatible_options(settings.openai_compatible_model,
                                             settings.openai_compatible_label)

    if getattr(settings, "claude_enabled", False):
        options += claude_options(settings.claude_model,
                                  "Vertex AI" if settings.claude_backend == "vertex" else "Anthropic API")

    if settings.default_generator != "codex":
        # 既定はCodexが先頭に来る並び順とCodex自身のis_defaultで決まる。
        # 別のproviderを既定にしたいときだけ、ここで明示的に付け替える。
        # 該当providerの選択肢が無ければ何もしない（並び順どおりCodexに戻る）。
        for option in options:
            if option["provider"] == settings.default_generator:
                for other in options:
                    other["is_default"] = False
                option["is_default"] = True
                break

    return {"models": options}


@router.post("/login")
async def login(request: Request):
    user_id, allowed = await run_in_threadpool(access, request)

    if not allowed:
        raise HTTPException(403, "この利用者はCodexを使用できません。管理者に確認してください。")

    result = await controller(request.app.state.settings, user_id, "POST", "/login")

    await run_in_threadpool(audit, request, user_id, "codex.login_started")

    return result


@router.post("/logout")
async def logout(request: Request):
    user_id, allowed = await run_in_threadpool(access, request)

    if not allowed:
        raise HTTPException(403, "この利用者はCodexを使用できません。管理者に確認してください。")
    result = await controller(request.app.state.settings, user_id, "POST", "/logout")

    await run_in_threadpool(audit, request, user_id, "codex.disconnected")

    return result
