"""テナントの生成アプリが使うGemini。管理者が1回設定すれば、アプリ側は何も知らずに使える。

生成アプリへは google-genai が引数なしで読む環境変数の形で渡す。
  Gemini API … GOOGLE_GENAI_USE_VERTEXAI=false と GEMINI_API_KEY（Secret経由）
  Vertex AI  … GOOGLE_GENAI_USE_VERTEXAI=true、GOOGLE_CLOUD_PROJECT / LOCATION、
               GOOGLE_APPLICATION_CREDENTIALS（Workload Identity連携の設定ファイル）
どちらも GEMINI_MODEL と GEMINI_THINKING_LEVEL を足す。

ここで作るのはテナントの既定値。プロジェクトの環境変数に同じ名前があれば、そちらが勝つ。
"""
from __future__ import annotations

import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.core import secret_box

BACKENDS = ("none", "gemini_api", "vertex")
THINKING_LEVELS = ("MINIMAL", "LOW", "MEDIUM", "HIGH")

# プレビューのPodに置く場所。preview-controller が投影ボリュームで同じ場所へ載せる。
CREDENTIAL_DIR = "/var/run/koyorina/gcp"
TOKEN_FILE = "token"
CONFIG_FILE = "credential-config.json"

# 生成アプリへ渡す名前。AGENTS.md の「LLM を使う機能」と揃えること（テストで確かめる）。
USE_VERTEX = "GOOGLE_GENAI_USE_VERTEXAI"
API_KEY = "GEMINI_API_KEY"
PROJECT = "GOOGLE_CLOUD_PROJECT"
LOCATION = "GOOGLE_CLOUD_LOCATION"
CREDENTIALS = "GOOGLE_APPLICATION_CREDENTIALS"
MODEL = "GEMINI_MODEL"
THINKING = "GEMINI_THINKING_LEVEL"
NAMES = (USE_VERTEX, API_KEY, PROJECT, LOCATION, CREDENTIALS, MODEL, THINKING)

GCP_PROJECT = r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$"
LOCATION_PATTERN = r"^[a-z][a-z0-9-]{1,40}$"
MODEL_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,99}$"
POOL_PATTERN = r"^[a-z0-9-]{4,32}$"
SERVICE_ACCOUNT = r"^[a-z0-9-]{6,30}@[a-z0-9-]{6,30}\.iam\.gserviceaccount\.com$"


class TenantAiError(ValueError):
    """画面へそのまま出せる日本語の理由を持つ。"""


class TenantAiInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    backend: Literal["none", "gemini_api", "vertex"] = "none"
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

    @model_validator(mode="after")
    def complete(self):
        if self.backend == "none":
            return self
        if not re.fullmatch(MODEL_PATTERN, self.model):
            raise TenantAiError("モデル名を入力してください（例：gemini-3.5-flash）。")
        if self.api_key is not None and (not self.api_key or re.search(r"\s", self.api_key)):
            raise TenantAiError("APIキーの形式が正しくありません。")
        if self.backend == "vertex":
            checks = [
                (GCP_PROJECT, self.gcp_project, "GCPプロジェクトID"),
                (LOCATION_PATTERN, self.location, "リージョン（例：global、us-central1）"),
                (r"^\d{6,20}$", self.wif_project_number, "Workload Identity プールのあるプロジェクトの番号"),
                (POOL_PATTERN, self.wif_pool_id, "Workload Identity プールID"),
                (POOL_PATTERN, self.wif_provider_id, "プロバイダーID"),
            ]
            for pattern, value, label in checks:
                if not re.fullmatch(pattern, value):
                    raise TenantAiError(f"{label}を正しく入力してください。")
            if self.wif_service_account and not re.fullmatch(SERVICE_ACCOUNT, self.wif_service_account):
                raise TenantAiError("なりすまし先のサービスアカウントは xxx@project.iam.gserviceaccount.com の形で入力してください。")
        return self


def apply(row, payload: TenantAiInput, secret_key: str) -> None:
    """保存済みの行へ反映する。APIキーは暗号化し、使わない方式では残さない。"""
    row.backend = payload.backend
    row.model, row.thinking_level = payload.model, payload.thinking_level
    vertex = payload.backend == "vertex"
    row.gcp_project = payload.gcp_project if vertex else ""
    row.location = payload.location if vertex else ""
    row.wif_project_number = payload.wif_project_number if vertex else ""
    row.wif_pool_id = payload.wif_pool_id if vertex else ""
    row.wif_provider_id = payload.wif_provider_id if vertex else ""
    row.wif_service_account = payload.wif_service_account if vertex else ""
    if payload.backend != "gemini_api":
        # 使わない秘密を持ち続けない。
        row.api_key_encrypted = None
        return
    if payload.api_key is not None:
        try:
            row.api_key_encrypted = secret_box.seal(secret_key, payload.api_key)
        except secret_box.SecretBoxUnavailable as exc:
            raise TenantAiError("APIキーを暗号化する鍵（TENANT_SECRET_KEY）が設定されていないため、"
                                "保存できません。管理者に設定を依頼するか、Vertex AI を選んでください。") from exc
    if not row.api_key_encrypted:
        raise TenantAiError("Gemini API のAPIキーを入力してください。")


def visible(row, secret_key: str) -> dict:
    """画面へ返す形。APIキーは値を出さず、設定済みかどうかだけ伝える。"""
    return {
        "backend": getattr(row, "backend", "none") or "none",
        "api_key_configured": bool(getattr(row, "api_key_encrypted", None)),
        "gcp_project": getattr(row, "gcp_project", ""),
        "location": getattr(row, "location", ""),
        "model": getattr(row, "model", ""),
        "thinking_level": getattr(row, "thinking_level", ""),
        "wif_project_number": getattr(row, "wif_project_number", ""),
        "wif_pool_id": getattr(row, "wif_pool_id", ""),
        "wif_provider_id": getattr(row, "wif_provider_id", ""),
        "wif_service_account": getattr(row, "wif_service_account", ""),
        # 鍵が無い環境では、Gemini API を選ばせても保存できない。画面で先に知らせる。
        "secrets_available": secret_box.available(secret_key),
    }


def pool_path(row) -> str:
    return (f"projects/{row.wif_project_number}/locations/global/workloadIdentityPools/"
            f"{row.wif_pool_id}/providers/{row.wif_provider_id}")


def token_audience(row) -> str:
    """Kubernetesのトークンに入れるaud。GCPのOIDCプロバイダーが既定で受け付ける値。"""
    return "https://iam.googleapis.com/" + pool_path(row)


def credential_config(row, token_file: str = f"{CREDENTIAL_DIR}/{TOKEN_FILE}") -> dict:
    """google-auth が読む external_account の設定。秘密は含まない（トークンは別ファイル）。

    row は wif_project_number / wif_pool_id / wif_provider_id / wif_service_account を持つもの。
    Koyorina 本体のエージェント用（token_file が別の場所）とテナント用で共通に使う。
    """
    config = {
        "universe_domain": "googleapis.com",
        "type": "external_account",
        "audience": "//iam.googleapis.com/" + pool_path(row),
        "subject_token_type": "urn:ietf:params:oauth:token-type:jwt",
        "token_url": "https://sts.googleapis.com/v1/token",
        "credential_source": {"file": token_file, "format": {"type": "text"}},
    }
    if row.wif_service_account:
        config["service_account_impersonation_url"] = (
            "https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/"
            f"{row.wif_service_account}:generateAccessToken")
    return config


def environment(row, secret_key: str) -> tuple[dict, dict, dict | None]:
    """生成アプリへ渡す（平文の環境変数, 秘密の環境変数, WIFの設定）。

    秘密は preview-controller が Kubernetes の Secret に入れ、secretKeyRef で渡す。
    Deploymentの定義に平文で残さないため、平文と分けて返す。
    """
    backend = getattr(row, "backend", "none") if row is not None else "none"
    if backend not in ("gemini_api", "vertex"):
        return {}, {}, None
    plain = {MODEL: row.model}
    if row.thinking_level:
        plain[THINKING] = row.thinking_level
    if backend == "gemini_api":
        plain[USE_VERTEX] = "false"
        secret = {}
        if row.api_key_encrypted:
            try:
                secret[API_KEY] = secret_box.open_(secret_key, row.api_key_encrypted)
            except secret_box.SecretBoxUnavailable:
                # 開けないキーは渡さない。アプリ側はGeminiの機能だけが止まる。
                secret = {}
        return plain, secret, None
    plain.update({USE_VERTEX: "true", PROJECT: row.gcp_project, LOCATION: row.location,
                  CREDENTIALS: f"{CREDENTIAL_DIR}/{CONFIG_FILE}"})
    wif = {"audience": token_audience(row), "config": json.dumps(credential_config(row))}
    return plain, {}, wif


def layered(tenant: tuple[dict, dict, dict | None], project_env: dict) -> tuple[dict, dict, dict | None]:
    """テナントの既定値の上に、プロジェクトの環境変数を重ねる。

    プロジェクトで同じ名前を指定していれば、テナントの値（秘密も含む）は渡さない。
    資格情報のファイルを自分で指定したプロジェクトには、WIFの設定を付けない。
    """
    plain, secret, wif = tenant
    extra = {**{k: v for k, v in plain.items() if k not in project_env}, **project_env}
    secret = {k: v for k, v in secret.items() if k not in project_env}
    if CREDENTIALS in project_env:
        wif = None
    return extra, secret, wif


def llm_summary(row) -> dict | None:
    """生成AIへ伝える分。秘密も接続先も入れない。「使ってよい」ことだけ分かればよい。"""
    backend = getattr(row, "backend", "none") if row is not None else "none"
    if backend not in ("gemini_api", "vertex"):
        return None
    return {"available": True, "backend": backend}
