"""テナントの生成AI。システム管理者は全テナント、テナント管理者は自分が管理者のテナントだけ扱える。

保存すると codex-controller へ送り、そのテナントの生成の実行環境は、使っていないものから
すぐに入れ替わる（生成中のものは終わりしだい）。
"""
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

from backend.api.masters import existing_tenant, tenant_administrator
from backend.core import gemini_client, k8s_token
from backend.core.db import Audit, SystemSetting, TenantLlmSetting
from backend.core.generation_client import controller_system
from backend.domain import system_gemini, tenant_ai, tenant_llm

router = APIRouter(prefix="/api/tenants")


def secret_key(settings) -> str:
    return settings.tenant_secret_key.get_secret_value()


def system_values(db) -> dict:
    """比べるために添えるシステムの既定。"""
    rows = {row.key: row.value for row in db.scalars(select(SystemSetting).where(
        SystemSetting.key.in_((system_gemini.KEY, *tenant_llm.KINDS[1:]))))}
    return {kind: rows.get(system_gemini.KEY if kind == "gemini" else kind) for kind in tenant_llm.KINDS}


def tenant_rows(db, tenant_id) -> dict:
    return {row.kind: row.value for row in db.scalars(
        select(TenantLlmSetting).where(TenantLlmSetting.tenant_id == str(tenant_id)))}


def generation_settings(request: Request, tenant_id=None):
    """生成の選択肢を出すための設定。システム設定を重ね、テナントがあればさらに重ねる。"""
    settings = gemini_client.effective(request.app.state.settings)
    if tenant_id is None:
        return settings
    with request.app.state.sessions() as db:
        rows = tenant_rows(db, tenant_id)
    update = tenant_llm.overrides(rows, secret_key(request.app.state.settings))
    return settings.model_copy(update=update) if update else settings


@router.get("/{tenant_id}/llm")
def llm_settings(tenant_id: UUID, request: Request):
    settings = request.app.state.settings
    with request.app.state.sessions() as db:
        tenant_administrator(request, db, tenant_id)
        existing_tenant(db, tenant_id)
        rows, defaults = tenant_rows(db, tenant_id), system_values(db)
        return {kind: tenant_llm.visible(kind, rows.get(kind), defaults[kind], settings, secret_key(settings))
                for kind in tenant_llm.KINDS}


@router.put("/{tenant_id}/llm/{kind}")
async def llm_update(tenant_id: UUID, kind: str, payload: tenant_llm.TenantLlmUpdate, request: Request):
    if kind not in tenant_llm.KINDS:
        raise HTTPException(404, "設定が見つかりません。")
    settings = request.app.state.settings
    try:
        parsed = tenant_llm.parse(kind, payload.settings or {}) if payload.mode == "tenant" else None
    except tenant_ai.TenantAiError as exc:
        raise HTTPException(422, str(exc)) from None

    def save():
        with request.app.state.sessions() as db:
            admin = tenant_administrator(request, db, tenant_id)
            existing_tenant(db, tenant_id)
            row = db.get(TenantLlmSetting, (str(tenant_id), kind))
            if payload.mode == "system":
                if row is not None:
                    db.delete(row)
            else:
                row = row or TenantLlmSetting(tenant_id=str(tenant_id), kind=kind)
                try:
                    row.value = (tenant_llm.DISABLED if payload.mode == "disabled"
                                 else tenant_llm.apply(kind, row.value, parsed, secret_key(settings)))
                except tenant_ai.TenantAiError as exc:
                    raise HTTPException(422, str(exc)) from None
                db.add(row)
            # 値は監査に残さない。どの扱いにしたかだけでよい。
            db.add(Audit(actor_id=admin.id, action="tenant.llm_updated", resource_id=str(tenant_id),
                         detail=f"kind={kind}; mode={payload.mode}"))
            db.commit()
            rows = tenant_rows(db, tenant_id)
            result = tenant_llm.visible(kind, rows.get(kind), system_values(db)[kind], settings,
                                        secret_key(settings))
            return rows, result
    rows, result = await run_in_threadpool(save)
    synced = None
    if settings.codex_controller_url:
        try:
            await controller_system(settings, "PUT", f"/settings/tenants/{tenant_id}/llm",
                                    tenant_llm.controller_payload(rows, secret_key(settings)), timeout=30)
            synced = True
        except HTTPException:
            # 保存は済んでいる。生成エージェントへ届かなかったことだけ伝え、再保存で送り直せる。
            synced = False
    return {**result, "agents_synced": synced}


@router.get("/{tenant_id}/llm/workload-identity")
async def llm_workload_identity(tenant_id: UUID, request: Request):
    """テナントの GCP 側で信頼を登録するための値。生成エージェントの身元、発行元、公開鍵。"""
    settings = request.app.state.settings

    def check():
        with request.app.state.sessions() as db:
            tenant_administrator(request, db, tenant_id)
            existing_tenant(db, tenant_id)
    await run_in_threadpool(check)

    def read():
        try:
            discovery = k8s_token.cluster_get("/.well-known/openid-configuration")
            return {"agent_subject": tenant_llm.agent_subject(settings, tenant_id),
                    "issuer": discovery.get("issuer", ""), "jwks": k8s_token.cluster_get("/openid/v1/jwks")}
        except (k8s_token.TokenUnavailable, OSError):
            raise HTTPException(409, "Kubernetes 上で動いていないため、クラスタの情報を読めません。") from None
    return await run_in_threadpool(read)
