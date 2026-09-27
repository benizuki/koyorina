"""システム設定の Antigravity・OpenAI 互換 API・Claude。画面で指定した値が、環境の設定より優先される。

Gemini（domain/system_gemini）と同じ考え方にする。
  - まだ保存していなければ、環境の設定（ANTIGRAVITY_ENABLED・OPENAI_COMPATIBLE_* など）のまま。
  - 保存したら、その値で有効／無効を含めて上書きする。
  - APIキーは暗号化して保存し、画面には「設定済みか」だけを返す。
使う場所は2つ。本体は選べるモデルの一覧に、生成エージェントは codex-controller 経由で
次に作る Pod の環境変数に反映する。
"""
from __future__ import annotations

import re
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

from backend.core import secret_box
from backend.domain import tenant_ai

ANTIGRAVITY = "antigravity"
OPENAI_COMPATIBLE = "openai_compatible"
CLAUDE = "claude"
KEYS = (ANTIGRAVITY, OPENAI_COMPATIBLE, CLAUDE)
AGENT_PATTERN = r"^[a-z0-9][a-z0-9.-]{0,99}$"


def _check_key(api_key: str | None):
    if api_key is not None and (not api_key or re.search(r"\s", api_key)):
        raise tenant_ai.TenantAiError("APIキーの形式が正しくありません。")


def valid_base_url(value: str) -> bool:
    """接続先。認証情報やクエリを URL に埋め込ませない（ログや画面に出るため）。"""
    endpoint = urlparse(value)
    return (endpoint.scheme in {"http", "https"} and bool(endpoint.hostname)
            and not (endpoint.username or endpoint.password or endpoint.query or endpoint.fragment))


class AntigravityInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    enabled: bool
    model: str = Field(default="", max_length=100)
    agent: str = Field(default="", max_length=100)
    max_total_tokens: int = Field(default=50000, ge=1000, le=200000)
    # None は「変えない」。Antigravity は Gemini API（Developer API）のキーで動く。
    api_key: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def complete(self):
        _check_key(self.api_key)
        if not self.enabled:
            return self
        if not re.fullmatch(tenant_ai.MODEL_PATTERN, self.model):
            raise tenant_ai.TenantAiError("モデル名を入力してください（例：gemini-3.8-flash）。")
        if not re.fullmatch(AGENT_PATTERN, self.agent):
            raise tenant_ai.TenantAiError("Antigravity のエージェント名を入力してください。")
        return self


class OpenAICompatibleInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    enabled: bool
    label: str = Field(default="", max_length=40)
    base_url: str = Field(default="", max_length=300)
    model: str = Field(default="", max_length=100)
    api_key: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def complete(self):
        _check_key(self.api_key)
        if not self.enabled:
            return self
        if not valid_base_url(self.base_url):
            raise tenant_ai.TenantAiError(
                "接続先のURLを http(s)://ホスト名/パス の形で入力してください（認証情報やクエリは含めません）。")
        if not re.fullmatch(tenant_ai.MODEL_PATTERN, self.model):
            raise tenant_ai.TenantAiError("モデル名を入力してください。")
        return self


class ClaudeInput(BaseModel):
    """Claude（Claude Agent SDK）。Claude on Vertex AI か Anthropic の APIキー。

    Vertex AI の認証は、生成の実行環境の Gemini と同じ（鍵ファイルか Workload Identity 連携）。
    プロジェクトとリージョンは Claude 用に別に指定できる（Claude を有効にしたプロジェクト）。
    """
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    enabled: bool
    backend: Literal["api_key", "vertex"] = "api_key"
    model: str = Field(default="", max_length=100)
    gcp_project: str = Field(default="", max_length=64)
    location: str = Field(default="", max_length=64)
    api_key: str | None = Field(default=None, max_length=300)

    @model_validator(mode="after")
    def complete(self):
        _check_key(self.api_key)
        if not self.enabled:
            return self
        if not re.fullmatch(tenant_ai.MODEL_PATTERN, self.model):
            raise tenant_ai.TenantAiError("モデル名を入力してください（例：claude-sonnet-5）。")
        if self.backend == "vertex":
            if not re.fullmatch(tenant_ai.GCP_PROJECT, self.gcp_project):
                raise tenant_ai.TenantAiError("Claude を使う GCP プロジェクトID を正しく入力してください。")
            if not re.fullmatch(tenant_ai.LOCATION_PATTERN, self.location):
                raise tenant_ai.TenantAiError("リージョンを正しく入力してください（例：global、us-east5）。")
        return self


INPUTS = {ANTIGRAVITY: AntigravityInput, OPENAI_COMPATIBLE: OpenAICompatibleInput, CLAUDE: ClaudeInput}


def apply(stored: dict | None, payload: AntigravityInput | OpenAICompatibleInput, secret_key: str,
          *, key_required: bool) -> dict:
    """保存する値を作る。APIキーは暗号化し、送られてこなければ前の値を残す。"""
    value = {**payload.model_dump(exclude={"api_key"}), "saved": True}
    previous = (stored or {}).get("api_key_encrypted")
    if payload.api_key is not None:
        try:
            value["api_key_encrypted"] = secret_box.seal(secret_key, payload.api_key)
        except secret_box.SecretBoxUnavailable as exc:
            raise tenant_ai.TenantAiError("APIキーを暗号化する鍵（TENANT_SECRET_KEY）が設定されていないため、"
                                          "保存できません。運用担当者に設定を依頼してください。") from exc
    else:
        value["api_key_encrypted"] = previous
    if payload.enabled and key_required and not value["api_key_encrypted"]:
        raise tenant_ai.TenantAiError("APIキーを入力してください。")
    return value


def _open(secret_key: str, sealed: str | None) -> str:
    if not sealed:
        return ""
    try:
        return secret_box.open_(secret_key, sealed)
    except secret_box.SecretBoxUnavailable:
        return ""  # 開けないキーは使わない。その経路だけが止まる


def environment(kind: str, settings) -> dict:
    """保存前に使っている環境の設定。画面で「いまはこれ」と示すため。秘密は含めない。"""
    if kind == ANTIGRAVITY:
        return {"enabled": bool(getattr(settings, "antigravity_enabled", False)),
                "model": getattr(settings, "antigravity_model", "")}
    if kind == CLAUDE:
        return {"enabled": bool(getattr(settings, "claude_enabled", False)),
                "model": getattr(settings, "claude_model", ""),
                "backend": getattr(settings, "claude_backend", "api_key")}
    return {"enabled": bool(getattr(settings, "openai_compatible_enabled", False)),
            "label": getattr(settings, "openai_compatible_label", ""),
            "base_url": getattr(settings, "openai_compatible_base_url", ""),
            "model": getattr(settings, "openai_compatible_model", "")}


def visible(kind: str, value: dict | None, settings, secret_key: str) -> dict:
    """画面へ返す形。APIキーは値を出さない。"""
    current = {k: v for k, v in (value or {}).items() if k != "api_key_encrypted"}
    return {**current, "saved": bool(value), "api_key_configured": bool((value or {}).get("api_key_encrypted")),
            "secrets_available": secret_box.available(secret_key),
            "environment": environment(kind, settings)}


def overrides(values: dict, secret_key: str) -> dict:
    """環境の設定（Settings）へ重ねる値。保存していない種類は何も変えない。"""
    update = {}
    if antigravity := values.get(ANTIGRAVITY):
        update.update(antigravity_enabled=bool(antigravity.get("enabled")),
                      antigravity_model=antigravity.get("model") or "")
    if claude := values.get(CLAUDE):
        update.update(claude_enabled=bool(claude.get("enabled")), claude_model=claude.get("model") or "",
                      claude_backend=claude.get("backend") or "api_key")
    if compatible := values.get(OPENAI_COMPATIBLE):
        update.update(openai_compatible_enabled=bool(compatible.get("enabled")),
                      openai_compatible_label=compatible.get("label") or "OpenAI互換API",
                      openai_compatible_base_url=compatible.get("base_url") or "",
                      openai_compatible_model=compatible.get("model") or "",
                      openai_compatible_api_key=SecretStr(_open(secret_key, compatible.get("api_key_encrypted"))))
    return update


def agent_payload(kind: str, value: dict | None, secret_key: str) -> dict:
    """codex-controller へ送る形。保存していなければ空（環境の設定のまま）。"""
    if not value:
        return {}
    result = {k: v for k, v in value.items() if k not in {"api_key_encrypted", "saved", "label"}}
    result["api_key"] = _open(secret_key, value.get("api_key_encrypted"))
    return result
