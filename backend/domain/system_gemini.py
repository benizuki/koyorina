"""Koyorina 自身が使う Gemini（システム設定）。画面で指定した値が、環境の設定より優先される。

使う場所は2つ。
  本体        … PDF読取・目的の下書き・音声入力。gemini_client がこの設定を重ねる。
  生成エージェント … Gemini での生成・ヒアリング。codex-controller へ送り、次に作る Pod から効く。

画面で選べるのは Gemini API（APIキー）か Vertex AI（Workload Identity 連携）の2択。
まだ保存していないとき（backend="env"）だけ、環境の設定（GEMINI_API_BACKEND・VERTEX_PROJECT など）を使う。
WIF のトークンの渡し方は2通り。
  本体        … 自分の ServiceAccount のトークンを TokenRequest API で都度取得（Pod を作り直さない）
  生成エージェント … モデルがコマンドを実行する Pod なので Kubernetes API のトークンは渡さない。
                codex-controller が投影ボリュームで audience を限ったトークンを載せる。
"""
from __future__ import annotations

import json
import re
from types import SimpleNamespace
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

from backend.core import secret_box
from backend.domain import tenant_ai

KEY = "gemini"
# 生成の選択肢に出すモデルID。画面の選択肢・生成の依頼の両方で同じ形を使う。
GENERATION_MODEL = r"[a-z][a-z0-9.\-]{0,40}"
MAX_GENERATION_MODELS = 20
# 一覧から外すもの。生成（コードを書く）に使えない用途のモデル。
# 名前で見分けるしかない（API は用途を返さない）。新しい種類が出たらここに足す。
NOT_FOR_GENERATION = ("embedding", "tts", "image", "banana", "live", "audio", "transcribe", "aqa", "robotics",
                      "computer-use")
# 生成エージェントでの置き場。鍵ファイルと同じ /run/vertex に載せる。
AGENT_DIR = "/run/vertex"
AGENT_TOKEN = f"{AGENT_DIR}/token"
AGENT_CONFIG = f"{AGENT_DIR}/credential-config.json"


class SystemGeminiInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    backend: Literal["gemini_api", "vertex"]
    # None は「変えない」。秘密は画面へ返していないので、送られてこなければ前の値を残す。
    api_key: str | None = Field(default=None, max_length=200)
    gcp_project: str = Field(default="", max_length=64)
    location: str = Field(default="", max_length=64)
    model: str = Field(default="", max_length=100)
    thinking_level: Literal["", "MINIMAL", "LOW", "MEDIUM", "HIGH"] = ""
    wif_project_number: str = Field(default="", max_length=20)
    wif_pool_id: str = Field(default="", max_length=64)
    wif_provider_id: str = Field(default="", max_length=64)
    wif_service_account: str = Field(default="", max_length=200)
    # 生成の選択肢に出すモデル。空なら環境の設定（GEMINI_MODELS）のまま。
    generation_models: list[str] = Field(default_factory=list, max_length=MAX_GENERATION_MODELS)

    @model_validator(mode="after")
    def complete(self):
        for name in self.generation_models:
            if not re.fullmatch(GENERATION_MODEL, name):
                raise tenant_ai.TenantAiError(f"選択肢に出すモデル名の形式が正しくありません（{name[:40]}）。")
        self.generation_models = list(dict.fromkeys(self.generation_models))
        if not re.fullmatch(tenant_ai.MODEL_PATTERN, self.model):
            raise tenant_ai.TenantAiError("モデル名を入力してください（例：gemini-3.5-flash）。")
        if self.api_key is not None and (not self.api_key or re.search(r"\s", self.api_key)):
            raise tenant_ai.TenantAiError("APIキーの形式が正しくありません。")
        if self.backend != "vertex":
            return self
        checks = [(tenant_ai.GCP_PROJECT, self.gcp_project, "GCPプロジェクトID"),
                  (tenant_ai.LOCATION_PATTERN, self.location, "リージョン（例：global、us-central1）"),
                  (r"^\d{6,20}$", self.wif_project_number, "Workload Identity プールのあるプロジェクトの番号"),
                  (tenant_ai.POOL_PATTERN, self.wif_pool_id, "Workload Identity プールID"),
                  (tenant_ai.POOL_PATTERN, self.wif_provider_id, "プロバイダーID")]
        for pattern, value, label in checks:
            if not re.fullmatch(pattern, value):
                raise tenant_ai.TenantAiError(f"{label}を正しく入力してください。")
        if self.wif_service_account and not re.fullmatch(
                tenant_ai.SERVICE_ACCOUNT, self.wif_service_account):
            raise tenant_ai.TenantAiError(
                "なりすまし先のサービスアカウントは xxx@project.iam.gserviceaccount.com の形で入力してください。")
        return self


# 保存していないときの値。backend="env" は環境の設定を使う。
DEFAULTS = {"backend": "env", "gcp_project": "", "location": "", "model": "", "thinking_level": "",
            "wif_enabled": False, "wif_project_number": "", "wif_pool_id": "", "wif_provider_id": "",
            "wif_service_account": "", "generation_models": []}


def apply(stored: dict | None, payload: SystemGeminiInput, secret_key: str) -> dict:
    """保存する値を作る。APIキーは暗号化し、使わない方式では残さない。"""
    previous = stored or {}
    # Vertex AI は必ず WIF。wif_enabled は、WIF 無しで保存していた以前の値と見分けるために残す。
    value = {**payload.model_dump(exclude={"api_key"}), "wif_enabled": payload.backend == "vertex"}
    if payload.backend != "vertex":
        value.update(gcp_project="", location="", wif_enabled=False, wif_project_number="",
                     wif_pool_id="", wif_provider_id="", wif_service_account="")
    if payload.backend != "gemini_api":
        value["api_key_encrypted"] = None
        return value
    if payload.api_key is not None:
        try:
            value["api_key_encrypted"] = secret_box.seal(secret_key, payload.api_key)
        except secret_box.SecretBoxUnavailable as exc:
            raise tenant_ai.TenantAiError("APIキーを暗号化する鍵（TENANT_SECRET_KEY）が設定されていないため、"
                                          "保存できません。Vertex AI を選ぶか、運用担当者に設定を依頼してください。") from exc
    else:
        value["api_key_encrypted"] = previous.get("api_key_encrypted")
    if not value["api_key_encrypted"]:
        raise tenant_ai.TenantAiError("Gemini API のAPIキーを入力してください。")
    return value


def stored(value: dict | None) -> dict:
    """保存済みの値を、欠けた項目を補った形で返す。検査を強めたあとの古い値でも開けるように。"""
    base = DEFAULTS
    return {**base, "api_key_encrypted": None, **{k: v for k, v in (value or {}).items() if k in base
                                                  or k == "api_key_encrypted"}}


def visible(value: dict | None, settings, secret_key: str) -> dict:
    """画面へ返す形。APIキーは値を出さない。環境の設定（backend=env のときに使う値）も添える。"""
    current = stored(value)
    return {**{k: v for k, v in current.items() if k != "api_key_encrypted"},
            "api_key_configured": bool(current["api_key_encrypted"]),
            "secrets_available": secret_box.available(secret_key),
            "environment": {"backend": "gemini_api" if settings.gemini_api_backend == "developer" else "vertex",
                            "gcp_project": settings.vertex_project, "location": settings.vertex_location,
                            "model": settings.vertex_model,
                            "generation_models": [name.strip() for name in settings.gemini_models.split(",")
                                                  if name.strip()]}}


def wif(value: dict | None) -> SimpleNamespace | None:
    """有効な Workload Identity 連携の設定。Vertex AI で WIF を選んでいるときだけ。"""
    current = stored(value)
    if current["backend"] != "vertex" or not current["wif_enabled"]:
        return None
    return SimpleNamespace(**{k: current[k] for k in (
        "wif_project_number", "wif_pool_id", "wif_provider_id", "wif_service_account")})


def audience(settings: SimpleNamespace) -> str:
    return tenant_ai.token_audience(settings)


def credential_config(settings: SimpleNamespace, token_file: str = AGENT_TOKEN) -> dict:
    return tenant_ai.credential_config(settings, token_file)


def overrides(value: dict | None, secret_key: str) -> dict:
    """環境の設定（Settings）へ重ねる値。backend=env なら何も変えない。"""
    current = stored(value)
    if current["backend"] == "env":
        return {}
    update = {"vertex_model": current["model"], "gemini_thinking_level": current["thinking_level"]}
    if current["generation_models"]:
        update["gemini_models"] = ",".join(current["generation_models"])
    if current["backend"] == "gemini_api":
        key = ""
        if current["api_key_encrypted"]:
            try:
                key = secret_box.open_(secret_key, current["api_key_encrypted"])
            except secret_box.SecretBoxUnavailable:
                key = ""  # 開けないキーは使わない。Gemini を使う機能だけが止まる
        return {**update, "gemini_api_backend": "developer", "gemini_api_key": SecretStr(key)}
    return {**update, "gemini_api_backend": "vertex", "vertex_project": current["gcp_project"],
            "vertex_location": current["location"]}


def agent_payload(value: dict | None, secret_key: str) -> dict:
    """codex-controller へ送る形。backend=env なら空（環境の設定のまま）。"""
    current = stored(value)
    if current["backend"] == "env":
        return {}
    result = {"backend": "developer" if current["backend"] == "gemini_api" else "vertex",
              "model": current["model"], "project": current["gcp_project"], "location": current["location"]}
    if current["backend"] == "gemini_api":
        result["api_key"] = overrides(value, secret_key)["gemini_api_key"].get_secret_value()
    if (settings := wif(value)) is not None:
        result.update(audience=audience(settings), config=json.dumps(credential_config(settings)))
    return result


def generation_candidates(models) -> list[dict]:
    """Gemini の models.list の結果から、生成の選択肢にできるものだけを残す。

    API で呼べるモデルには、埋め込み・画像・音声など生成（コードを書く）に使えないものが混ざる。
    推論の段階は API から分からない（対応しているかの真偽だけ）ので、対応表（GEMINI_THINKING_LEVELS）
    に無いモデルは段階を選ばせない。
    """
    found = {}
    for model in models:
        identifier = str(getattr(model, "name", "") or "").rsplit("/", 1)[-1]
        actions = getattr(model, "supported_actions", None)
        if (not identifier.startswith("gemini-") or not re.fullmatch(GENERATION_MODEL, identifier)
                or any(word in identifier for word in NOT_FOR_GENERATION)
                or (actions is not None and "generateContent" not in actions)):
            continue
        found[identifier] = {"id": identifier,
                             "label": str(getattr(model, "display_name", "") or identifier)[:80],
                             "thinking": bool(getattr(model, "thinking", False))}
    return sorted(found.values(), key=lambda item: item["id"], reverse=True)
