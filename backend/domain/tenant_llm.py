"""テナントの生成AI（生成に使う Gemini・Antigravity・OpenAI互換）。

値の形はシステム設定と同じで、domain/system_gemini・system_llm の関数をそのまま使う。
行が無い種類はシステムの既定を使う。{"disabled": true} はそのテナントでは使わせない。

本体の機能（PDF読取・目的の下書き・音声入力）はテナントが決まる前にも使うので、ここは関係しない。
生成したアプリが使う Gemini（domain/tenant_ai）とも別の設定。
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, ValidationError
from pydantic import SecretStr

from backend.domain import system_gemini, system_llm, tenant_ai
from backend.domain.tenant_storage import canonical_tenant_id, tenant_hash

KINDS = ("gemini", system_llm.ANTIGRAVITY, system_llm.OPENAI_COMPATIBLE, system_llm.CLAUDE)
DISABLED = {"disabled": True}


class TenantLlmUpdate(BaseModel):
    """画面から来る形。system=システムの既定を使う / tenant=このテナントの設定 / disabled=使わせない。"""
    model_config = ConfigDict(extra="forbid")
    mode: Literal["system", "tenant", "disabled"]
    settings: dict[str, Any] | None = None


def parse(kind: str, values: dict[str, Any]):
    """種類ごとの入力。文面はこちらが書いたもの（TenantAiError）だけを返す。"""
    model = system_gemini.SystemGeminiInput if kind == "gemini" else system_llm.INPUTS[kind]
    try:
        return model.model_validate(values)
    except ValidationError as exc:
        reasons = [error.get("ctx", {}).get("error") for error in exc.errors()]
        message = next((str(reason) for reason in reasons if isinstance(reason, tenant_ai.TenantAiError)),
                       "入力内容を確認してください。")
        raise tenant_ai.TenantAiError(message) from None


def apply(kind: str, stored: dict | None, parsed, secret_key: str) -> dict:
    previous = None if stored == DISABLED else stored
    if kind == "gemini":
        return system_gemini.apply(previous, parsed, secret_key)
    return system_llm.apply(previous, parsed, secret_key, key_required=key_required(kind, parsed))


def key_required(kind: str, parsed) -> bool:
    """APIキーが要るか。OpenAI互換は必ず、Claude は APIキーの方式のときだけ。"""
    return kind == system_llm.OPENAI_COMPATIBLE or (kind == system_llm.CLAUDE and parsed.backend == "api_key")


def mode(value: dict | None) -> str:
    return "system" if value is None else "disabled" if value == DISABLED else "tenant"


def visible(kind: str, value: dict | None, system_value: dict | None, settings, secret_key: str) -> dict:
    """画面へ返す形。テナントの値と、比べられるようにシステムの既定を添える。キーの値は出さない。"""
    def shown(current):
        if kind == "gemini":
            return system_gemini.visible(current, settings, secret_key)
        return system_llm.visible(kind, current, settings, secret_key)
    return {"kind": kind, "mode": mode(value),
            "settings": shown(value) if mode(value) == "tenant" else None,
            "system": shown(system_value)}


def controller_payload(rows: dict[str, dict], secret_key: str) -> dict:
    """codex-controller へ送る形（controller の TenantLlmInput）。キーはここで復号して渡す。"""
    payload: dict[str, Any] = {"disabled": []}
    for kind in KINDS:
        value = rows.get(kind)
        if value is None:
            continue
        if value == DISABLED:
            payload["disabled"].append(kind)
        elif kind == "gemini":
            payload["gemini"] = system_gemini.agent_payload(value, secret_key) or None
        else:
            payload[kind] = system_llm.agent_payload(kind, value, secret_key) or None
    return payload


def overrides(rows: dict[str, dict], secret_key: str) -> dict:
    """本体の設定（Settings）へ重ねる値。開発画面のモデルの選択肢をテナントに合わせるために使う。"""
    update: dict[str, Any] = {}
    gemini = rows.get("gemini")
    if gemini == DISABLED:
        update.update(gemini_api_backend="vertex", vertex_project="", gemini_api_key=SecretStr(""))
    elif gemini is not None:
        update.update(system_gemini.overrides(gemini, secret_key))
    for kind in (system_llm.ANTIGRAVITY, system_llm.OPENAI_COMPATIBLE, system_llm.CLAUDE):
        value = rows.get(kind)
        if value == DISABLED:
            update[f"{kind}_enabled"] = False
        elif value is not None:
            update.update(system_llm.overrides({kind: value}, secret_key))
    return update


def agent_subject(settings, tenant_id) -> str:
    """テナントで Vertex AI（WIF）を使うとき、生成エージェントが名乗る身元（controller と同じ名前）。"""
    tenant = canonical_tenant_id(tenant_id)
    return (f"system:serviceaccount:{settings.app_name}-codex:"
            f"{settings.app_name}-agent-t-{tenant_hash(tenant, 16)}")
