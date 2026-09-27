"""システム設定。本体と生成エージェントが使う生成AI（Gemini・Antigravity・OpenAI互換API）を画面から変える。

保存すると、本体は次の呼び出しから新しい設定を使う（gemini_client のキャッシュを捨てる）。
生成エージェントへは codex-controller へ送り、次に作る Pod から効く。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from google.auth.exceptions import RefreshError
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from backend.core import gemini_client, k8s_token
from backend.core.auth import actor
from backend.core.db import Audit, SystemSetting
from backend.core.generation_client import controller_system
from backend.domain import system_gemini, system_llm, tenant_ai, tenant_llm
from backend.domain.roles import can_manage

router = APIRouter(prefix="/api/system")
PATH = "/gemini"


def administrator(request, db):
    user = actor(request, db)
    if not can_manage(user):
        raise HTTPException(403, "管理者だけが利用できます。")
    return user


def agent_subject(settings) -> str:
    return f"system:serviceaccount:{settings.app_name}-codex:{settings.app_name}-codex-agent"


def secret_key(settings) -> str:
    return settings.tenant_secret_key.get_secret_value()


@router.get(PATH)
def gemini(request: Request):
    settings = request.app.state.settings
    with request.app.state.sessions() as db:
        administrator(request, db)
        row = db.get(SystemSetting, system_gemini.KEY)
        return system_gemini.visible(row.value if row else None, settings, secret_key(settings))


@router.put(PATH)
async def gemini_update(payload: system_gemini.SystemGeminiInput, request: Request):
    settings = request.app.state.settings

    def save():
        with request.app.state.sessions() as db:
            admin = administrator(request, db)
            row = db.get(SystemSetting, system_gemini.KEY) or SystemSetting(key=system_gemini.KEY)
            try:
                row.value = system_gemini.apply(row.value, payload, secret_key(settings))
            except tenant_ai.TenantAiError as exc:
                raise HTTPException(422, str(exc)) from None
            db.add(row)
            # 値は監査に残さない。どの方式にしたかだけでよい。
            db.add(Audit(actor_id=admin.id, action="system.gemini_updated", resource_id=system_gemini.KEY,
                         detail=f"backend={payload.backend}"))
            db.commit()
            return row.value
    value = await run_in_threadpool(save)
    gemini_client.refresh_system_settings()
    synced = None
    if settings.codex_controller_url:
        try:
            await controller_system(settings, "PUT", "/settings/gemini",
                                    system_gemini.agent_payload(value, secret_key(settings)), timeout=30)
            synced = True
        except HTTPException:
            # 保存は済んでいる。生成エージェントへ届かなかったことだけ伝え、再保存で送り直せる。
            synced = False
    return {**system_gemini.visible(value, settings, secret_key(settings)), "agents_synced": synced}


@router.post(PATH + "/test")
async def gemini_test(request: Request):
    """本体の身元と、保存済みの設定で Gemini に1回だけ問い合わせる。"""
    settings = request.app.state.settings

    def check():
        with request.app.state.sessions() as db:
            return administrator(request, db).id
    admin_id = await run_in_threadpool(check)
    if not gemini_client.available(settings) or not gemini_client.model(settings):
        raise HTTPException(409, "Gemini の接続先またはモデルが設定されていません。")

    def ask():
        from backend.domain import gemini_probe
        chosen = gemini_client.model(settings)
        try:
            config = {"thinking_config": gemini_client.thinking_config(settings)}
            # クライアントは変数に持ち続ける。google-genai の Client は参照が切れると __del__ で
            # 接続を閉じるので、client(...).models.generate_content(...) と続けて書くと、
            # 呼ぶ前に閉じられて「client has been closed」になる（実際に起きた）。
            api = gemini_client.client(settings, timeout=30_000)
            try:
                answer = api.models.generate_content(
                    model=chosen, contents=gemini_probe.PROMPT,
                    config={k: v for k, v in config.items() if v is not None} or None)
            finally:
                api.close()
            return {"ok": True, "step": "generate", "model": chosen, "text": (answer.text or "").strip()[:200]}
        except k8s_token.TokenUnavailable as exc:
            return {"ok": False, "step": "credentials", "message": str(exc)}
        except RefreshError as exc:
            # STSでの交換、またはなりすましで断られた。プロバイダーの条件やIAMを見直す段階。
            return {"ok": False, "step": "sts", "message": str(exc)[:500]}
        except Exception as exc:  # google-genai の例外は種類が多い。理由の文面だけ返す
            return {"ok": False, "step": "generate", "message": str(exc)[:500]}
    result = await run_in_threadpool(ask)

    def record():
        with request.app.state.sessions() as db:
            db.add(Audit(actor_id=admin_id, action="system.gemini_tested", resource_id=system_gemini.KEY,
                         detail=f"ok={bool(result.get('ok'))}; step={result.get('step')}"))
            db.commit()
    await run_in_threadpool(record)
    return result


@router.get(PATH + "/workload-identity")
async def gemini_identity(request: Request):
    """GCP側で信頼を登録するための値。本体とエージェントの身元、発行元、公開鍵。"""
    settings = request.app.state.settings

    def check():
        with request.app.state.sessions() as db:
            administrator(request, db)
    await run_in_threadpool(check)

    def read():
        try:
            discovery = k8s_token.cluster_get("/.well-known/openid-configuration")
            return {"app_subject": k8s_token.subject(), "agent_subject": agent_subject(settings),
                    "issuer": discovery.get("issuer", ""), "jwks": k8s_token.cluster_get("/openid/v1/jwks")}
        except (k8s_token.TokenUnavailable, OSError):
            raise HTTPException(409, "Kubernetes 上で動いていないため、クラスタの情報を読めません。") from None
    return await run_in_threadpool(read)


# Antigravity と OpenAI 互換 API。URL の綴りと、保存する鍵・controller の送り先を対応させる。
LLM_PATHS = {"antigravity": system_llm.ANTIGRAVITY, "openai-compatible": system_llm.OPENAI_COMPATIBLE,
             "claude": system_llm.CLAUDE}


def llm_kind(name: str) -> str:
    if name not in LLM_PATHS:
        raise HTTPException(404, "設定が見つかりません。")
    return LLM_PATHS[name]


@router.get("/llm/{name}")
def llm_settings(name: str, request: Request):
    kind = llm_kind(name)
    settings = request.app.state.settings
    with request.app.state.sessions() as db:
        administrator(request, db)
        row = db.get(SystemSetting, kind)
        return system_llm.visible(kind, row.value if row else None, settings, secret_key(settings))


@router.put("/llm/{name}")
async def llm_update(name: str, payload: dict, request: Request):
    kind = llm_kind(name)
    settings = request.app.state.settings
    try:
        parsed = system_llm.INPUTS[kind].model_validate(payload)
    except ValidationError as exc:
        # 文面はこちらが書いたもの（TenantAiError）だけを返す。pydantic の既定の文面には入力値が入る。
        reasons = [error.get("ctx", {}).get("error") for error in exc.errors()]
        message = next((str(reason) for reason in reasons if isinstance(reason, tenant_ai.TenantAiError)),
                       "入力内容を確認してください。")
        raise HTTPException(422, message) from None

    def save():
        with request.app.state.sessions() as db:
            admin = administrator(request, db)
            row = db.get(SystemSetting, kind) or SystemSetting(key=kind)
            try:
                # OpenAI 互換 API はキーが要る（Claude は APIキーの方式のとき）。
                # Antigravity は未入力なら環境のキーを使う。
                row.value = system_llm.apply(row.value, parsed, secret_key(settings),
                                             key_required=tenant_llm.key_required(kind, parsed))
            except tenant_ai.TenantAiError as exc:
                raise HTTPException(422, str(exc)) from None
            db.add(row)
            db.add(Audit(actor_id=admin.id, action=f"system.{kind}_updated", resource_id=kind,
                         detail=f"enabled={parsed.enabled}"))
            db.commit()
            return row.value
    value = await run_in_threadpool(save)
    gemini_client.refresh_system_settings()
    synced = None
    if settings.codex_controller_url:
        try:
            await controller_system(settings, "PUT", f"/settings/{name}",
                                    system_llm.agent_payload(kind, value, secret_key(settings)), timeout=30)
            synced = True
        except HTTPException:
            synced = False
    return {**system_llm.visible(kind, value, settings, secret_key(settings)), "agents_synced": synced}
