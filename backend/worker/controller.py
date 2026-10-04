"""Internal, least-privilege provisioner. Creates only fixed per-user agent manifests."""
import asyncio
import hashlib
import base64
import json
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timezone
import logging
import re
from pathlib import Path
import secrets
import ssl
from urllib.parse import quote, urlparse
from typing import Annotated, Literal
from uuid import UUID
import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic import BaseModel
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict
from backend.config.settings import SECRETS_DIR
from backend.core.request_log import RequestLogMiddleware, configure_logging
from backend.domain.generation import model_settings
from backend.domain.projects import ProjectInput
from backend.domain.tenant_copy import copy_script
from backend.domain.tenant_storage import (auth_worker, canonical_tenant_id,
                                           generation_claim, generation_worker,
                                           legacy_generation_worker,
                                           quantity_bytes, storage_measurement_pod,
                                           tenant_hash)
from backend.worker.agent import GenerationInput
from backend.domain.system_gemini import AGENT_CONFIG, AGENT_DIR, AGENT_TOKEN

ROUTES = ("codex", "gemini", "antigravity", "openai_compatible", "claude")
# Provider-specific workers existed before Codex/Gemini were consolidated into
# one tenant worker.  Antigravity was introduced after that migration and has
# never had a legacy worker name.
LEGACY_ROUTES = ("codex", "gemini")


class MigrationInput(BaseModel):
    migration_id: UUID
    project_id: UUID
    job_ids: list[UUID] = Field(default_factory=list, max_length=1000)
    source_tenant_id: UUID
    target_tenant_id: UUID
    legacy_source: bool = False


class SupportRevokeInput(BaseModel):
    actor_id: UUID
    tenant_id: UUID


def ready(pod) -> bool:
    """Podが要求を受けられる状態か。同じ判定をあちこちで書かない。"""
    return any(condition.get("type") == "Ready" and condition.get("status") == "True"
               for condition in (pod.get("status", {}).get("conditions") or []))


logger = logging.getLogger("koyorina.codex-controller")


class ControllerSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="controller_", extra="ignore", secrets_dir=SECRETS_DIR)
    token: SecretStr = Field(min_length=32)
    # Helmリリース名。許可イメージ名を固定するための運用設定。
    app_name: str = Field(default="koyorina", pattern=r"^[a-z]([a-z0-9-]*[a-z0-9])?$", max_length=44)
    # 既定は "koyorina-codex"。運営がアプリ名を変えた場合は、マニフェストが
    # CONTROLLER_NAMESPACE で実際のnamespaceを渡す（両者は${APP_NAME}から揃う）。
    namespace: str = Field(default="koyorina-codex", pattern=r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$",
                           max_length=63)
    # イメージの取得元。別の環境へ持っていくときは、ここと agent_image を揃えて変える。
    # 取得元を固定するのは、controllerに任意のイメージを起動させないため。
    # 既定は開発環境。実際の値はどちらの環境でもマニフェストのConfigMapが渡す
    # （setup/environments/<環境>.env の REGISTRY が正）。
    image_registry: str = Field(default="registry.example.com", max_length=200)
    agent_image: str = Field(default="", max_length=300)
    # kubernetes: 利用者ごとにPodを作る（本番）。compose: Docker Composeのお試し版。
    # k8sを使わず、composeが常駐させた1つのエージェントへ中継するだけ（1人用）。
    backend: Literal["kubernetes", "compose"] = "kubernetes"
    # composeのときだけ使う。常駐エージェントの宛先と、そのエージェントのAGENT_TOKEN。
    agent_url: str = Field(default="http://agent:8080", pattern=r"^http://[a-z0-9.-]+:8080$")
    agent_token: SecretStr = SecretStr("")
    # composeのときだけ使う。画面の生成AI設定（k8s版のConfigMap/Secretに当たるもの）と、
    # エージェントへ渡す環境変数を置く。エージェントと共有するボリューム。
    settings_dir: Path = Path("/settings")
    # 生成アプリの依存の取得元。生成Podへそのまま渡す。取得元を基盤が決めるのは、
    # 生成された宣言のなかで差し替えられるようにしないため。
    npm_registry: str = Field(default="", max_length=300)
    npm_min_release_age: str = Field(default="", max_length=20)
    pypi_index: str = Field(default="", max_length=300)
    # 生成Podを載せるノードと、作業領域のストレージクラス。クラスタごとに違う。
    # seccompプロファイルを置いたノードを指すこと（setup/manifest/seccomp/README.md）。
    # ノード固定型ストレージ（RWO）の既定はホスト名で1台へ寄せる。RWX（Filestore/NFS）
    # ではどのノードでもマウントできるので、node_selector でラベル一致に切り替えられる。
    node: str = Field(default="k3s-agent-2", max_length=253)
    # 空なら node を使う。RWX 環境では node を空にし、こちらへ
    # 例えば {"workload": "koyorina-agent"} のようなラベルを渡す（JSON文字列）。
    node_selector: Annotated[dict[str, str], NoDecode] = Field(default_factory=dict)

    @field_validator("node_selector", mode="before")
    @classmethod
    def _parse_node_selector(cls, value):
        """NoDecodeなので生の文字列で届く。環境変数は空文字が既定になりがちで、
        自動JSONデコードだと壊れて弾かれ気づきにくいため、ここで自前に解釈する。"""
        if value in ("", None) or value == {}:
            return {}
        if isinstance(value, str):
            import json
            return json.loads(value)
        return value
    environment: str = Field(default="dev", pattern=r"^[a-z0-9-]{1,30}$")
    network_policy_mode: Literal["legacy", "cilium"] = "legacy"
    audit_ca_configmap: str = Field(default="", max_length=253)
    # 生成Podの監査ログへ、サニタイズ済みの依頼概要を一時的に含める。
    # 既定は無効。環境ファイルからコントローラを経由して明示的に有効化する。
    audit_log_request_content: bool = False
    storage_class: str = Field(default="local-path", max_length=253)
    # 既定のストレージクラスはReadWriteOnceのノード固定型。Filestore/NFS 等の
    # RWX クラスへ切り替えると、テナントPVCを複数ノードへ跨いでマウントできる。
    storage_access_mode: Literal["ReadWriteOnce", "ReadWriteMany"] = "ReadWriteOnce"
    # 生成コード、ジョブ、履歴はテナントごとのPVCに置く。
    workspaces_size: str = Field(default="20Gi", max_length=20)
    # 旧共有PVCは移行Podだけが読む。通常の生成Podへマウントしない。
    legacy_workspaces_claim: str = Field(default="koyorina-workspaces",
                                         pattern=r"^[a-z0-9.-]+$", max_length=253)
    # 使われていない生成Podを片付けるまでの時間（分）。0 で回収しない。
    # Podは消しても失うものが無い。認証は利用者PVC、コードとジョブと履歴はテナントPVCに
    # あり、次に使うときコントローラが作り直して同じ続きから始まる。
    # 放置すると、一度も触っていない人のぶんまで要求（CPU 100m / メモリ 256Mi）を
    # 押さえ続け、利用者が増えたときに先に頭打ちになる。
    # ノードへ置いたAppArmorプロファイルの名前。空なら指定しない。
    # 生成コードの隔離は bwrap が担い、bwrap は mount を呼ぶ。containerd既定の
    # プロファイルはそれを拒むので、Ubuntu系のノードでは専用のものが要る。
    # AppArmorが無いノード（Rocky系）で指定すると、逆にPodが起動しなくなる。
    apparmor_profile: str = Field(default="", max_length=253)
    idle_minutes: int = Field(default=30, ge=0, le=1440)
    idle_sweep_seconds: int = Field(default=120, ge=10, le=3600)

    # エージェントPodへ渡す既定値。未指定ならCodexの既定に任せる。
    codex_model: str = ""
    codex_effort: str = ""
    # codex: 本人のChatGPT枠 / gemini: google-genai経由のGemini API
    generator: Literal["codex", "gemini", "antigravity", "openai_compatible", "claude"] = "codex"
    gemini_model: str = "gemini-3.8-flash"
    gemini_api_backend: Literal["vertex", "developer"] = "vertex"
    gemini_api_key_secret: str = Field(default="koyorina-gemini-api",
                                       pattern=r"^[a-z0-9.-]+$", max_length=253)
    antigravity_enabled: bool = False
    antigravity_agent: str = "antigravity-preview-09-2026"
    antigravity_model: str = "gemini-3.8-flash"
    antigravity_max_total_tokens: int = Field(default=50000, ge=1000, le=200000)
    # Claude（Claude Agent SDK）。画面（システム設定・テナント設定）で有効にする。
    claude_enabled: bool = False
    claude_backend: Literal["api_key", "vertex"] = "api_key"
    claude_model: str = Field(default="claude-sonnet-5", max_length=100)
    claude_project: str = Field(default="", max_length=64)
    claude_location: str = Field(default="global", max_length=64)
    claude_api_key_secret: str = Field(default="", pattern=r"^[a-z0-9.-]*$", max_length=253)
    openai_compatible_enabled: bool = False
    openai_compatible_base_url: str = Field(default="", max_length=300)
    openai_compatible_model: str = Field(default="", max_length=100)
    openai_compatible_api_key_secret: str = Field(default="koyorina-openai-compatible",
                                                   pattern=r"^[a-z0-9.-]+$", max_length=253)
    vertex_project: str = ""
    # gemini-3.8-flash はグローバルのみ提供される。
    vertex_location: str = "global"
    # 規約と共通部品をイメージ外から渡す場合のPVC名。AGENTS.md と scaffold/ を置く。
    # 指定するとagentはそちらを読むので、規約の更新にイメージの作り直しが要らない。
    # ConfigMapではなくPVCなのは、scaffold/ が入れ子のディレクトリだから。
    conventions_claim: str = ""
    # 画面（システム設定）で Gemini API を選んだときのキーの置き場。controller が作る。
    # 空なら環境の gemini_api_key_secret を使う（Antigravity はいつもそちら）。
    system_gemini_secret: str = Field(default="", pattern=r"^[a-z0-9.-]*$", max_length=253)
    # WIF で名乗る身元と、資格情報設定を置く ConfigMap。空ならシステム設定のもの。
    # テナントで Vertex AI（WIF）を設定すると、そのテナント専用のものに差し替わる。
    workload_service_account: str = Field(default="", pattern=r"^[a-z0-9.-]*$", max_length=253)
    workload_config_map: str = Field(default="", pattern=r"^[a-z0-9.-]*$", max_length=253)
    # 画面（システム設定）で Antigravity のキーを入れたときの置き場。空なら gemini_api_key_secret。
    antigravity_api_key_secret: str = Field(default="", pattern=r"^[a-z0-9.-]*$", max_length=253)
    image_pull_secret: str = Field(default="", pattern=r"^[a-z0-9.-]*$", max_length=253)
    agent_toleration: bool = False

    @property
    def agent_label(self) -> str:
        return f"{self.app_name}-codex-agent"

    @model_validator(mode="after")
    def supported_model_settings(self):
        if self.backend == "compose":
            # イメージはcomposeが手元でビルドして起動する。controllerは何も起動しない。
            if len(self.agent_token.get_secret_value()) < 32:
                raise ValueError("composeでは常駐エージェントの認証鍵(CONTROLLER_AGENT_TOKEN)が必要です。")
        else:
            # digest固定のまま、取得元だけを設定で変えられるようにする。
            expected = re.compile(re.escape(f"{self.image_registry}/{self.app_name}-agent")
                                  + r"@sha256:[0-9a-f]{64}$")
            if not expected.fullmatch(self.agent_image):
                raise ValueError("エージェントのイメージは CONTROLLER_IMAGE_REGISTRY のdigest参照で指定してください。")
        model_settings(self.codex_model, self.codex_effort)
        if self.openai_compatible_enabled and not (self.openai_compatible_base_url
                                                    and self.openai_compatible_model):
            raise ValueError("OpenAI互換APIを有効にするには接続先とモデルが必要です。")
        if self.openai_compatible_enabled:
            endpoint = urlparse(self.openai_compatible_base_url)
            if (endpoint.scheme not in {"http", "https"} or not endpoint.hostname
                    or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment):
                raise ValueError("OpenAI互換APIの接続先URLが不正です。")
        if (self.generator == "gemini" and self.gemini_api_backend == "vertex"
                and not self.vertex_project):
            raise ValueError("Gemini経路にはVertexのプロジェクトIDが必要です。")
        if (self.storage_access_mode == "ReadWriteOnce"
                and not self.node_selector and not self.node):
            # RWOはノード固定が前提。固定先が無いと、Podはどのノードにも
            # 安全にスケジュールできずPendingのまま終わらない。
            raise ValueError("ReadWriteOnce では node か node_selector のどちらかが必要です。")
        return self

    @property
    def effective_node_selector(self) -> dict[str, str]:
        """実際に使うnodeSelector。node_selectorがあればそちらを優先する。"""
        if self.node_selector:
            return dict(self.node_selector)
        if self.node:
            return {"kubernetes.io/hostname": self.node}
        return {}

    def scheduling(self, *, arch: bool = False) -> dict:
        """Pod specへ差し込むnodeSelector断片。セレクタが無ければ何も付けない。"""
        selector = self.effective_node_selector
        if arch:
            selector = {**selector, "kubernetes.io/arch": "amd64"}
        return {"nodeSelector": selector} if selector else {}


def gemini_configured(settings: ControllerSettings) -> bool:
    return settings.gemini_api_backend == "developer" or bool(settings.vertex_project)


def antigravity_configured(settings: ControllerSettings) -> bool:
    # The management app shares this helper but does not need Antigravity's
    # controller-only settings.  Treat absent fields as disabled instead of
    # breaking /api/codex/runtimes with an AttributeError.
    return bool(getattr(settings, "antigravity_enabled", False))


def gemini_environment(settings: ControllerSettings, wif: dict | None = None):
    """共有生成Podだけへ、選択したGemini APIの認証を渡す。"""
    antigravity = ([
        {"name": "AGENT_ANTIGRAVITY_ENABLED", "value": "true"},
        {"name": "AGENT_ANTIGRAVITY_AGENT", "value": settings.antigravity_agent},
        {"name": "AGENT_ANTIGRAVITY_MODEL", "value": settings.antigravity_model},
        {"name": "AGENT_ANTIGRAVITY_MAX_TOTAL_TOKENS", "value": str(settings.antigravity_max_total_tokens)},
    ] if settings.antigravity_enabled else [])
    # AntigravityはVertex経路と独立したDeveloper APIを使う。画面で専用のキーを入れていれば
    # 別の変数で渡す（GEMINI_API_KEY に載せると Gemini API 経路のキーと取り合いになる）。
    # 入れていなければ、環境の Gemini API キーの Secret を共有する。
    if settings.antigravity_enabled and settings.antigravity_api_key_secret:
        antigravity_key = [{"name": "AGENT_ANTIGRAVITY_API_KEY", "valueFrom": {"secretKeyRef": {
            "name": settings.antigravity_api_key_secret, "key": "GEMINI_API_KEY"}}}]
    elif settings.antigravity_enabled:
        # 環境のキーは配備時に作っていないことがある（画面で入れる運用）。無くても Pod は起動させる。
        antigravity_key = [{"name": "GEMINI_API_KEY", "valueFrom": {"secretKeyRef": {
            "name": settings.gemini_api_key_secret, "key": "GEMINI_API_KEY", "optional": True}}}]
    else:
        antigravity_key = []
    compatible = ([
        {"name": "AGENT_OPENAI_COMPATIBLE_ENABLED", "value": "true"},
        {"name": "AGENT_OPENAI_COMPATIBLE_BASE_URL", "value": settings.openai_compatible_base_url},
        {"name": "AGENT_OPENAI_COMPATIBLE_MODEL", "value": settings.openai_compatible_model},
        {"name": "AGENT_OPENAI_COMPATIBLE_API_KEY", "valueFrom": {"secretKeyRef": {
            "name": settings.openai_compatible_api_key_secret, "key": "OPENAI_COMPATIBLE_API_KEY"}}},
    ] if settings.openai_compatible_enabled else [])
    # Claude。APIキーは専用の変数で渡す（ANTHROPIC_API_KEY にはしない。SDK へ渡すのは claude_agent）。
    compatible += ([
        {"name": "AGENT_CLAUDE_ENABLED", "value": "true"},
        {"name": "AGENT_CLAUDE_BACKEND", "value": settings.claude_backend},
        {"name": "AGENT_CLAUDE_MODEL", "value": settings.claude_model},
        {"name": "AGENT_CLAUDE_PROJECT", "value": settings.claude_project},
        {"name": "AGENT_CLAUDE_LOCATION", "value": settings.claude_location},
        *([{"name": "AGENT_CLAUDE_API_KEY", "valueFrom": {"secretKeyRef": {
            "name": settings.claude_api_key_secret, "key": "ANTHROPIC_API_KEY"}}}]
          if settings.claude_backend == "api_key" and settings.claude_api_key_secret else []),
    ] if settings.claude_enabled else [])
    if settings.gemini_api_backend == "developer":
        base = [
            {"name": "GEMINI_API_BACKEND", "value": "developer"},
            {"name": "GEMINI_API_KEY", "valueFrom": {"secretKeyRef": {
                "name": settings.system_gemini_secret or settings.gemini_api_key_secret,
                "key": "GEMINI_API_KEY", "optional": not settings.system_gemini_secret}}},
        ]
        # GEMINI_API_KEY は Gemini API 経路のものを渡している。重ねて渡さない。
        return base + [item for item in antigravity_key if item["name"] != "GEMINI_API_KEY"] \
            + antigravity + compatible
    antigravity = antigravity_key + antigravity
    if not settings.vertex_project:
        return antigravity + compatible
    # 画面（システム設定）で WIF を有効にしていれば、鍵ファイルは使わない。
    # 認証は鍵ファイルか WIF のどちらか（トークンファイル方式は廃止。生成の SDK が読めない）。
    credential = {"name": "GOOGLE_APPLICATION_CREDENTIALS",
                  "value": AGENT_CONFIG if wif else "/run/vertex/key.json"}
    return [{"name": "GEMINI_API_BACKEND", "value": "vertex"},
            {"name": "GOOGLE_GENAI_USE_VERTEXAI", "value": "1"},
            {"name": "GOOGLE_CLOUD_PROJECT", "value": settings.vertex_project},
            {"name": "GOOGLE_CLOUD_LOCATION", "value": settings.vertex_location},
            credential] + antigravity + compatible


def llm_fingerprint(settings: ControllerSettings, wif: dict | None = None) -> str:
    """生成AIの設定の指紋。Pod に付けておき、設定が変わったら入れ替える目印にする。

    秘密の値そのものは入らない（Secret の名前と参照だけ）。
    """
    material = {"env": gemini_environment(settings, wif), "generator": settings.generator,
                "gemini_model": settings.gemini_model, "wif": (wif or {}).get("audience", "")}
    return hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()[:16]


LLM_ANNOTATION = "koyorina/llm-settings"


def package_environment(settings: ControllerSettings):
    """依存の取得元。値を持つものだけ渡す（未設定なら既定の公開レジストリ）。"""
    values = {"AGENT_NPM_REGISTRY": settings.npm_registry,
              "AGENT_NPM_MIN_RELEASE_AGE": settings.npm_min_release_age,
              "AGENT_PYPI_INDEX": settings.pypi_index}
    return [{"name": name, "value": value} for name, value in values.items() if value]


def conventions_environment(settings: ControllerSettings):
    """規約をイメージ外から読ませる。未指定ならイメージ同梱のまま。"""
    if not settings.conventions_claim:
        return []
    return [{"name": "CONVENTIONS_ROOT", "value": "/run/conventions"}]


def worker_route(settings: ControllerSettings, requested=None):
    requested = requested or settings.generator
    if requested == "codex":
        return "codex"
    if requested == "antigravity":
        if not antigravity_configured(settings):
            raise HTTPException(503, "Antigravityの実行環境が未設定です。")
        return "antigravity"
    if requested == "claude":
        if not settings.claude_enabled:
            raise HTTPException(503, "Claudeの実行環境が未設定です。")
        return "claude"
    if requested == "openai_compatible":
        if not settings.openai_compatible_enabled:
            raise HTTPException(503, "OpenAI互換APIの実行環境が未設定です。")
        return "openai_compatible"
    if not gemini_configured(settings):
        raise HTTPException(503, "Geminiの実行環境が未設定です。")
    return settings.generator if settings.generator != "codex" else "gemini"


# resources() の鍵は、そのままKubernetesのリソース種別として使う。
# 同じ種別を2つ返す場合だけ、ここで読み替える。
RESOURCE_KINDS = {"workspaces": "persistentvolumeclaims"}


def worker_name(user_id: UUID, route: str, tenant: str | UUID | None = None):
    """Authentication workers never receive tenant storage; generation workers always do."""
    if tenant is None:
        if route != "codex":
            raise ValueError("tenant is required for generation workers")
        return auth_worker(user_id)
    return generation_worker(user_id, tenant)


class SystemGeminiInput(BaseModel):
    """画面（システム設定）の Gemini。backend が空なら「環境の設定のまま」。
    audience が空なら Vertex AI は環境の鍵ファイルで認証する。"""
    backend: Literal["", "vertex", "developer"] = ""
    model: str = Field(default="", pattern=r"^[A-Za-z0-9._/-]{0,100}$")
    project: str = Field(default="", pattern=r"^([a-z][a-z0-9-]{4,28}[a-z0-9])?$")
    location: str = Field(default="", pattern=r"^([a-z][a-z0-9-]{1,40})?$")
    api_key: SecretStr = SecretStr("")
    audience: str = Field(default="", max_length=300)
    config: str = Field(default="", max_length=4096)

    @model_validator(mode="after")
    def trusted_shape(self):
        if self.backend == "vertex" and not (self.project and self.location):
            raise ValueError("Vertex AI のプロジェクトとリージョンが必要です。")
        if self.backend == "developer" and not self.api_key.get_secret_value():
            raise ValueError("Gemini API のキーが必要です。")
        if not self.audience:
            return self
        if self.backend != "vertex":
            raise ValueError("Workload Identity 連携は Vertex AI のときだけ使えます。")
        if not re.fullmatch(r"https://iam\.googleapis\.com/projects/\d{6,20}/locations/global/"
                            r"workloadIdentityPools/[a-z0-9-]{4,32}/providers/[a-z0-9-]{4,32}", self.audience):
            raise ValueError("Workload Identity の audience が不正です。")
        try:
            config = json.loads(self.config)
        except ValueError:
            raise ValueError("Workload Identity の設定が不正です。") from None
        source = config.get("credential_source") if isinstance(config, dict) else None
        if (config.get("type") != "external_account"
                or config.get("token_url") != "https://sts.googleapis.com/v1/token"
                or not isinstance(source, dict) or source.get("file") != AGENT_TOKEN
                or "//" + self.audience.removeprefix("https://") != config.get("audience")):
            raise ValueError("Workload Identity の設定が不正です。")
        return self


class SystemAntigravityInput(BaseModel):
    """画面（システム設定）の Antigravity。enabled が None なら消す（環境の設定へ戻る）。"""
    enabled: bool | None = None
    model: str = Field(default="", pattern=r"^[A-Za-z0-9._/-]{0,100}$")
    agent: str = Field(default="", pattern=r"^([a-z0-9][a-z0-9.-]{0,99})?$")
    max_total_tokens: int = Field(default=50000, ge=1000, le=200000)
    api_key: SecretStr = SecretStr("")

    @model_validator(mode="after")
    def trusted_shape(self):
        if self.enabled and not (self.model and self.agent):
            raise ValueError("Antigravity のモデルとエージェント名が必要です。")
        return self


class SystemOpenAICompatibleInput(BaseModel):
    """画面（システム設定）の OpenAI 互換 API。enabled が None なら消す（環境の設定へ戻る）。"""
    enabled: bool | None = None
    base_url: str = Field(default="", max_length=300)
    model: str = Field(default="", pattern=r"^[A-Za-z0-9._/-]{0,100}$")
    api_key: SecretStr = SecretStr("")

    @model_validator(mode="after")
    def trusted_shape(self):
        if not self.enabled:
            return self
        endpoint = urlparse(self.base_url)
        if (endpoint.scheme not in {"http", "https"} or not endpoint.hostname
                or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment):
            raise ValueError("OpenAI互換APIの接続先URLが不正です。")
        if not self.model or not self.api_key.get_secret_value():
            raise ValueError("OpenAI互換APIのモデルとAPIキーが必要です。")
        return self


class SystemClaudeInput(BaseModel):
    """画面（システム設定・テナント設定）の Claude。enabled が None なら消す（環境の設定へ戻る）。"""
    enabled: bool | None = None
    backend: Literal["api_key", "vertex"] = "api_key"
    model: str = Field(default="", pattern=r"^[A-Za-z0-9._@/-]{0,100}$")
    gcp_project: str = Field(default="", pattern=r"^([a-z][a-z0-9-]{4,28}[a-z0-9])?$")
    location: str = Field(default="", pattern=r"^([a-z][a-z0-9-]{1,40})?$")
    api_key: SecretStr = SecretStr("")

    @model_validator(mode="after")
    def trusted_shape(self):
        if not self.enabled:
            return self
        if not self.model:
            raise ValueError("Claude のモデルが必要です。")
        if self.backend == "vertex" and not (self.gcp_project and self.location):
            raise ValueError("Claude on Vertex AI のプロジェクトとリージョンが必要です。")
        if self.backend == "api_key" and not self.api_key.get_secret_value():
            raise ValueError("Claude の APIキーが必要です。")
        return self


class TenantLlmInput(BaseModel):
    """テナントの生成AI。書かれた種類だけテナントの値を使い、無い種類はシステムの既定を使う。

    disabled に入れた種類は、そのテナントでは使わせない。
    """
    gemini: SystemGeminiInput | None = None
    antigravity: SystemAntigravityInput | None = None
    openai_compatible: SystemOpenAICompatibleInput | None = None
    claude: SystemClaudeInput | None = None
    disabled: list[Literal["gemini", "antigravity", "openai_compatible", "claude"]] = Field(default_factory=list)


# 画面で保存した生成AIの設定を置く ConfigMap／Secret の名前の後ろ半分と、Secret の鍵名。
SYSTEM_LLM = {"antigravity": ("system-antigravity", "GEMINI_API_KEY"),
              "openai-compatible": ("system-openai-compatible", "OPENAI_COMPATIBLE_API_KEY"),
              "claude": ("system-claude", "ANTHROPIC_API_KEY")}


def system_llm_name(settings: ControllerSettings, kind: str) -> str:
    return f"{settings.app_name}-{SYSTEM_LLM[kind][0]}"


def agent_service_account(settings: ControllerSettings) -> str:
    """WIFで名乗る生成エージェントの身元。GCP側はこの名前（subject）を信頼する。"""
    return settings.workload_service_account or f"{settings.app_name}-codex-agent"


def tenant_llm_name(settings: ControllerSettings, tenant, kind: str = "") -> str:
    """テナントの生成AIの設定を置く ConfigMap（kind なし）と、種類ごとの Secret の名前。"""
    base = f"{settings.app_name}-tenant-llm-{tenant_hash(tenant, 16)}"
    return f"{base}-{kind}" if kind else base


def tenant_agent_service_account(settings: ControllerSettings, tenant) -> str:
    """テナントで Vertex AI（WIF）を使うときに名乗る身元。テナントの GCP 側はこれを信頼する。"""
    return f"{settings.app_name}-agent-t-{tenant_hash(tenant, 16)}"


def system_gemini_name(settings: ControllerSettings) -> str:
    """画面（システム設定）の Gemini を置く ConfigMap と Secret の名前。"""
    return f"{settings.app_name}-system-gemini"


def wif_config_name(settings: ControllerSettings) -> str:
    return settings.workload_config_map or system_gemini_name(settings)


def llm_overlay(values: dict, names: dict) -> tuple[dict, dict | None, bool]:
    """画面で保存した生成AIの設定を、環境の設定（ControllerSettings）へ重ねる値にする。

    values は種類ごとの辞書（ConfigMap の値。文字列）。names は種類ごとの Secret の名前と、
    テナントのときだけ WIF の ConfigMap（config_map）と身元（service_account）。
    戻り値は (重ねる値, WIF, Gemini を決めたか)。
    """
    update = {}
    antigravity = values.get("antigravity") or {}
    if antigravity.get("enabled") in {"true", "false"}:
        update["antigravity_enabled"] = antigravity["enabled"] == "true"
        if antigravity.get("model"):
            update["antigravity_model"] = antigravity["model"]
        if antigravity.get("agent"):
            update["antigravity_agent"] = antigravity["agent"]
        if (antigravity.get("max_total_tokens") or "").isdigit():
            update["antigravity_max_total_tokens"] = int(antigravity["max_total_tokens"])
        if antigravity.get("has_key") == "true":
            update["antigravity_api_key_secret"] = names["antigravity"]
    compatible = values.get("openai-compatible") or {}
    if compatible.get("enabled") in {"true", "false"}:
        update["openai_compatible_enabled"] = compatible["enabled"] == "true"
        if compatible["enabled"] == "true":
            update.update(openai_compatible_base_url=compatible.get("base_url", ""),
                          openai_compatible_model=compatible.get("model", ""),
                          openai_compatible_api_key_secret=names["openai-compatible"])
    claude = values.get("claude") or {}
    if claude.get("enabled") in {"true", "false"}:
        update["claude_enabled"] = claude["enabled"] == "true"
        if claude["enabled"] == "true":
            update.update(claude_backend=claude.get("backend") or "api_key",
                          claude_model=claude.get("model") or "claude-sonnet-5",
                          claude_project=claude.get("gcp_project", ""),
                          claude_location=claude.get("location") or "global",
                          claude_api_key_secret=names["claude"] if claude.get("has_key") == "true" else "")
    data = values.get("gemini") or {}
    if data.get("disabled") == "true":
        # 使わせない。Gemini の経路（とヒアリング）は「未設定」と同じ扱いになる。
        update.update(gemini_api_backend="vertex", vertex_project="")
        return update, None, True
    backend = data.get("backend", "")
    if backend not in {"vertex", "developer"}:
        return update, None, False
    update["gemini_api_backend"] = backend
    if data.get("model"):
        update["gemini_model"] = data["model"]
    if backend == "vertex":
        update.update(vertex_project=data.get("project", ""), vertex_location=data.get("location", ""))
    else:
        update["system_gemini_secret"] = names["gemini"]
    wif = ({"audience": data["audience"], "config": data["credential-config.json"]}
           if backend == "vertex" and data.get("audience") and data.get("credential-config.json") else None)
    if wif and names.get("service_account"):
        update.update(workload_service_account=names["service_account"],
                      workload_config_map=names["config_map"])
    return update, wif, True


def resources(user_id: UUID, settings: ControllerSettings, route=None,
              tenant: str | None = None, *, auth_only: bool = False, wif: dict | None = None):
    route = route or settings.generator
    # The authentication worker remains Codex-only and never receives tenant storage or
    # Gemini credentials. A generation worker supports both providers and chooses per request.
    if not auth_only and tenant is None:
        tenant = "00000000-0000-4000-8000-000000000001"
    if not auth_only:
        tenant = canonical_tenant_id(tenant)
    name = worker_name(user_id, route, None if auth_only else tenant)
    claim_name = f"codex-{user_id.hex}"
    # Generation workers are shared by both providers. Keep forge-route on authentication
    # workers for rollout compatibility and use shared on tenant workers.
    labels = {"app": settings.agent_label, "forge-user": str(user_id),
              "forge-route": "codex" if auth_only else "shared",
              "forge-network-policy": settings.network_policy_mode}
    if tenant:
        labels["forge-tenant"] = canonical_tenant_id(tenant)
    if auth_only:
        labels["forge-purpose"] = "authentication"
    meta = {"name": name, "namespace": settings.namespace, "labels": labels}
    if not auth_only:
        # 生成AIの設定が変わったら、使っていない Pod から入れ替える（reap_idle が見る）。
        meta["annotations"] = {LLM_ANNOTATION: llm_fingerprint(settings, wif)}
    security = {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                "capabilities": {"drop": ["ALL"]}}
    workspaces = generation_claim(tenant) if tenant else None
    vertex_wif = bool(wif and not auth_only and settings.gemini_api_backend == "vertex"
                      and settings.vertex_project)
    result = {
        # 利用者ごとのPVCはCodexの認証だけを持つ。コードはここに置かない。
        "persistentvolumeclaims": {"apiVersion": "v1", "kind": "PersistentVolumeClaim", "metadata": {**meta, "name": claim_name},
            "spec": {"accessModes": [settings.storage_access_mode], "storageClassName": settings.storage_class,
                     "resources": {"requests": {"storage": "2Gi"}}}},
        # テナント単位の作業場所。他テナントのPVCはPod仕様に現れない。
        "workspaces": {"apiVersion": "v1", "kind": "PersistentVolumeClaim",
            "metadata": {"name": workspaces, "namespace": settings.namespace},
            "spec": {"accessModes": [settings.storage_access_mode], "storageClassName": settings.storage_class,
                     "resources": {"requests": {"storage": settings.workspaces_size}}}},
        "pods": {"apiVersion": "v1", "kind": "Pod", "metadata": meta, "spec": {
            "automountServiceAccountToken": False, "enableServiceLinks": False,
            # WIFのときだけ専用の身元で動かす。Kubernetes API のトークンは載せない（上の false）。
            **({"serviceAccountName": agent_service_account(settings)} if vertex_wif else {}),
            # RWO（ノード固定型ストレージ）では固定した1ノードだけがプロファイルを持つ。RWX
            # (Filestore/NFS)では全ノードに配るので、node_selectorだけで足りる。
            # amd64は seccomp プロファイルが amd64 専用のため常に付ける。
            **settings.scheduling(arch=True),
            "securityContext": {"runAsNonRoot": True, "runAsUser": 10001, "runAsGroup": 10001,
                                "fsGroup": 10001,
                                # RWXでは毎回の再帰chownを避ける（node_modules等で数分かかる）。
                                "fsGroupChangePolicy": "OnRootMismatch",
                                "seccompProfile": {"type": "Localhost",
                                    "localhostProfile": "koyorina/codex-bwrap-amd64-v1.json"},
                                **({"appArmorProfile": {"type": "Localhost",
                                    "localhostProfile": settings.apparmor_profile}}
                                   if settings.apparmor_profile else {})},
            "containers": [{"name": "agent", "image": settings.agent_image,
                "securityContext": security, "ports": [{"containerPort": 8080}],
                "env": [{"name": "AGENT_USER_ID", "value": str(user_id)},
                        {"name": "AGENT_TOKEN", "valueFrom": {"secretKeyRef": {"name": name, "key": "token"}}},
                        {"name": "AGENT_CODEX_MODEL", "value": settings.codex_model},
                        {"name": "AGENT_CODEX_EFFORT", "value": settings.codex_effort},
                        {"name": "AGENT_GENERATOR", "value": settings.generator},
                        {"name": "AGENT_ENVIRONMENT", "value": settings.environment},
                        {"name": "AUDIT_LOG_REQUEST_CONTENT",
                         "value": "1" if settings.audit_log_request_content else "0"},
                        *([{"name": "AGENT_TENANT_ID", "value": tenant}] if tenant else []),
                        {"name": "AGENT_POD_NAME", "valueFrom": {"fieldRef": {
                            "fieldPath": "metadata.name"}}},
                        {"name": "AGENT_NAMESPACE", "valueFrom": {"fieldRef": {
                            "fieldPath": "metadata.namespace"}}},
                        {"name": "AGENT_GEMINI_MODEL", "value": settings.gemini_model},
                        *package_environment(settings),
                        *(gemini_environment(settings, wif if vertex_wif else None) if not auth_only else []),
                        *conventions_environment(settings)],
                # 依存の導入と vite build / vue-tsc をこのPodで走らせる。1Giだと
                # 型検査の途中でOOMになり、「生成が理由なく止まった」形で出る。
                # 置き場は対象テナントのPVCなので、ephemeral-storage はイメージぶんで足りる。
                "resources": {"requests": {"cpu": "100m", "memory": "256Mi"}, "limits": {"cpu": "2", "memory": "2Gi", "ephemeral-storage": "512Mi"}},
                # 認証は利用者のPVC、コードとジョブと変更履歴は対象テナントのPVC。混ぜない。
                # 履歴もアプリ単位。利用者のPVCや使い捨ての領域に置くと、担当が変わったり
                # Podが入れ替わった時点で消える（実際、配備のたびに消えていた）。
                "volumeMounts": [{"name": "data", "mountPath": "/data"}, {"name": "tmp", "mountPath": "/tmp"},
                                 {"name": "workspaces", "mountPath": "/data/projects", "subPath": "projects"},
                                 {"name": "workspaces", "mountPath": "/data/jobs", "subPath": "jobs"},
                                 {"name": "workspaces", "mountPath": "/data/history", "subPath": "history"},
                                 *([{"name": "vertex", "mountPath": "/run/vertex", "readOnly": True}]
                                   if (not auth_only and settings.gemini_api_backend == "vertex"
                                       and settings.vertex_project) else []),
                                 *([{"name": "conventions", "mountPath": "/run/conventions", "readOnly": True}]
                                   if settings.conventions_claim else [])],
                "readinessProbe": {"httpGet": {"path": "/healthz", "port": 8080}, "periodSeconds": 3},
                "livenessProbe": {"httpGet": {"path": "/healthz", "port": 8080}, "initialDelaySeconds": 20}}],
            "volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": claim_name}},
                        {"name": "workspaces", "persistentVolumeClaim": {"claimName": workspaces}},
                        {"name": "tmp", "emptyDir": {"sizeLimit": "128Mi"}},
                        *([{"name": "vertex", "projected": {"defaultMode": 0o444, "sources": [
                              # audience をKoyorinaのWIFプロバイダーに限ったトークン。GCPのSTSでしか使えない。
                              {"serviceAccountToken": {"audience": wif["audience"], "expirationSeconds": 3600,
                                                       "path": AGENT_TOKEN.removeprefix(AGENT_DIR + "/")}},
                              {"configMap": {"name": wif_config_name(settings), "items": [
                                  {"key": "credential-config.json",
                                   "path": AGENT_CONFIG.removeprefix(AGENT_DIR + "/")}]}}]}}]
                          if vertex_wif else
                          # 鍵ファイルは任意。WIF へ移る途中で鍵が無くても Pod は起動させ、
                          # 生成のときに「WIF を設定してください」と分かるように止める。
                          [{"name": "vertex", "secret": {"secretName": f"{settings.app_name}-vertex",
                                                         "optional": True}}]
                          if (not auth_only and settings.gemini_api_backend == "vertex"
                              and settings.vertex_project) else []),
                        *([{"name": "conventions", "persistentVolumeClaim": {
                            "claimName": settings.conventions_claim, "readOnly": True}}]
                          if settings.conventions_claim else []),
                        *([{"name": "audit-ca", "configMap": {
                                "name": settings.audit_ca_configmap}},
                           {"name": "audit-ca-bundle", "emptyDir": {"sizeLimit": "2Mi"}}]
                          if settings.audit_ca_configmap and not auth_only else [])]}}
    }

    if auth_only:
        # Login and model discovery only need the user's credential PVC. They must not be
        # able to observe any tenant workspace.
        result.pop("workspaces")
        container = result["pods"]["spec"]["containers"][0]
        container["volumeMounts"] = [mount for mount in container["volumeMounts"]
                                     if mount["name"] != "workspaces"]
        result["pods"]["spec"]["volumes"] = [volume for volume in result["pods"]["spec"]["volumes"]
                                               if volume["name"] != "workspaces"]

    if not auth_only:
        # subPath mounts require the directories to exist before the agent container starts.
        # Every tenant PVC is initially empty, so both Codex and Gemini need this initializer.
        shared = ("projects", "jobs", "history")
        result["pods"]["spec"]["initContainers"] = [{"name": "prepare-storage",
            "image": settings.agent_image, "securityContext": security,
            "command": ["python", "-c", "from pathlib import Path; " + "; ".join(
                f"Path('/data/{subpath}').mkdir(parents=True, exist_ok=True, mode=0o700)"
                for subpath in shared)],
            "volumeMounts": [{"name": "workspaces", "mountPath": "/data"}],
            "resources": {"requests": {"cpu": "10m", "memory": "32Mi"},
                          "limits": {"cpu": "100m", "memory": "64Mi"}}}]
        if settings.audit_ca_configmap:
            container = result["pods"]["spec"]["containers"][0]
            bundle = "/run/audit-ca-bundle/ca-bundle.crt"
            container["env"].extend([
                {"name": "NODE_EXTRA_CA_CERTS", "value": bundle},
                {"name": "SSL_CERT_FILE", "value": bundle},
                {"name": "REQUESTS_CA_BUNDLE", "value": bundle},
            ])
            container["volumeMounts"].append(
                {"name": "audit-ca-bundle", "mountPath": "/run/audit-ca-bundle",
                 "readOnly": True})
            result["pods"]["spec"]["initContainers"].append({
                "name": "prepare-audit-ca", "image": settings.agent_image,
                "securityContext": security,
                "command": ["/bin/sh", "-c",
                    "cat /etc/ssl/certs/ca-certificates.crt /run/audit-ca/ca.crt "
                    "> /run/audit-ca-bundle/ca-bundle.crt && chmod 0444 "
                    "/run/audit-ca-bundle/ca-bundle.crt"],
                "volumeMounts": [
                    {"name": "audit-ca", "mountPath": "/run/audit-ca", "readOnly": True},
                    {"name": "audit-ca-bundle", "mountPath": "/run/audit-ca-bundle"}],
                "resources": {"requests": {"cpu": "10m", "memory": "16Mi"},
                              "limits": {"cpu": "100m", "memory": "32Mi"}},
            })

    spec = result["pods"]["spec"]
    if settings.image_pull_secret:
        spec["imagePullSecrets"] = [{"name": settings.image_pull_secret}]
    if settings.agent_toleration:
        spec["tolerations"] = [{"key": "workload", "operator": "Equal",
                                "value": f"{settings.app_name}-agent", "effect": "NoSchedule"}]
    return result


class Provisioner:
    def __init__(self, settings):
        self.settings = settings

    async def kube(self, method, resource, name="", body=None, query="", text=False):
        token = Path("/var/run/secrets/kubernetes.io/serviceaccount/token").read_text().strip()
        context = ssl.create_default_context(cafile="/var/run/secrets/kubernetes.io/serviceaccount/ca.crt")
        url = f"https://kubernetes.default.svc/api/v1/namespaces/{self.settings.namespace}/{resource}"
        if name:
            url += "/" + name
        url += query
        async with httpx.AsyncClient(verify=context, timeout=15, trust_env=False) as client:
            response = await client.request(method, url, headers={"Authorization": "Bearer " + token}, json=body)
        if response.status_code == 404:
            return None
        if response.status_code == 409 and method == "POST":
            return await self.kube("GET", resource, body["metadata"]["name"])
        if not response.is_success:
            raise HTTPException(503, "AppGenの実行環境を準備できません。管理者に容量・権限の確認を依頼してください。")
        if text:
            return response.text
        return response.json() if response.content else {}

    async def measure_storage(self, tenant):
        """Measure one tenant claim without mounting any other tenant storage."""
        tenant = canonical_tenant_id(tenant)
        claim = generation_claim(tenant)
        pvc = await self.kube("GET", "persistentvolumeclaims", claim)
        base = {"kind": "generation", "claim_name": claim,
                "requested_bytes": quantity_bytes(self.settings.workspaces_size),
                "measured_at": datetime.now(timezone.utc).isoformat()}
        if pvc is None:
            return {**base, "status": "missing", "used_bytes": 0,
                    "capacity_bytes": 0, "available_bytes": 0}
        requested = (((pvc.get("spec") or {}).get("resources") or {}).get("requests") or {}).get("storage")
        if requested:
            base["requested_bytes"] = quantity_bytes(requested)
        name = "storage-generation-" + tenant_hash(tenant, 16)
        with suppress(Exception):
            await self.kube("DELETE", "pods", name)
        pod = storage_measurement_pod(name, self.settings.namespace, claim,
                                      self.settings.agent_image, self.settings.effective_node_selector,
                                      image_pull_secret=self.settings.image_pull_secret,
                                      toleration=self.settings.agent_toleration,
                                      app_name=self.settings.app_name)
        try:
            await self.kube("POST", "pods", body=pod)
            for _ in range(60):
                current = await self.kube("GET", "pods", name)
                phase = (current or {}).get("status", {}).get("phase")
                if phase == "Succeeded":
                    output = await self.kube("GET", "pods", name + "/log", text=True)
                    values = __import__("json").loads(output)
                    return {**base, "status": "ok", **values}
                if phase == "Failed":
                    raise HTTPException(503, "生成領域の使用量を計測できませんでした。")
                await asyncio.sleep(1)
            raise HTTPException(503, "生成領域の使用量計測が時間内に完了しませんでした。")
        finally:
            with suppress(Exception):
                await self.kube("DELETE", "pods", name)

    async def migrate(self, migration_id, project_id, job_ids, source_tenant, target_tenant,
                      *, legacy_source=False):
        """Copy one project's durable generation data without mounting unrelated tenants."""
        source_tenant, target_tenant = map(canonical_tenant_id, (source_tenant, target_tenant))
        # Ensure the target claim exists. A small fixed UUID is used only to obtain the claim
        # manifest; no worker or credential resource is created.
        target_claim = resources(UUID(int=0), self.settings, "codex", target_tenant)["workspaces"]
        if await self.kube("GET", "persistentvolumeclaims", target_claim["metadata"]["name"]) is None:
            await self.kube("POST", "persistentvolumeclaims", body=target_claim)
        name = "tenant-move-" + UUID(str(migration_id)).hex[:20]
        ids = [str(UUID(str(value))) for value in job_ids]
        project = str(UUID(str(project_id)))
        # 生成コード（依存の生成物は除く）・変更履歴・ジョブ記録。domain/tenant_copy 参照。
        script = copy_script([(f"projects/{project}", True), (f"history/{project}.git", False),
                              *[(f"jobs/{job}", False) for job in ids]])
        security = {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                    "capabilities": {"drop": ["ALL"]}}
        pod = {"apiVersion": "v1", "kind": "Pod", "metadata": {"name": name,
            "namespace": self.settings.namespace, "labels": {"app": "koyorina-tenant-migration"}},
            "spec": {"restartPolicy": "Never", "automountServiceAccountToken": False,
                **self.settings.scheduling(),
                "securityContext": {"runAsNonRoot": True, "runAsUser": 10001, "runAsGroup": 10001,
                                    "fsGroup": 10001, "fsGroupChangePolicy": "OnRootMismatch",
                                    "seccompProfile": {"type": "RuntimeDefault"}},
                "containers": [{"name": "copy", "image": self.settings.agent_image,
                    "command": ["python", "-c", script], "securityContext": security,
                    "resources": {"requests": {"cpu": "50m", "memory": "128Mi"},
                                  "limits": {"cpu": "1", "memory": "1Gi"}},
                    "volumeMounts": [{"name": "source", "mountPath": "/source", "readOnly": True},
                                     {"name": "target", "mountPath": "/target"},
                                     {"name": "tmp", "mountPath": "/tmp"}]}],
                "volumes": [{"name": "source", "persistentVolumeClaim": {"claimName":
                                self.settings.legacy_workspaces_claim if legacy_source else generation_claim(source_tenant)}},
                            {"name": "target", "persistentVolumeClaim": {"claimName": generation_claim(target_tenant)}},
                            {"name": "tmp", "emptyDir": {"sizeLimit": "64Mi"}}]}}
        if self.settings.image_pull_secret:
            pod["spec"]["imagePullSecrets"] = [{"name": self.settings.image_pull_secret}]
        if self.settings.agent_toleration:
            pod["spec"]["tolerations"] = [{"key": "workload", "operator": "Equal",
                "value": f"{self.settings.app_name}-agent", "effect": "NoSchedule"}]
        await self.kube("POST", "pods", body=pod)
        for _ in range(180):
            current = await self.kube("GET", "pods", name)
            phase = (current or {}).get("status", {}).get("phase")
            if phase == "Succeeded":
                await self.kube("DELETE", "pods", name)
                return {"status": "copied"}
            if phase == "Failed":
                raise HTTPException(503, "テナント間のデータコピーに失敗しました。")
            await asyncio.sleep(2)
        raise HTTPException(503, "テナント間のデータコピーが時間内に完了しませんでした。")

    async def cleanup_migration(self, migration_id, project_id, job_ids, source_tenant,
                                *, legacy_source=False):
        """Delete only one migrated project's retained source after its recovery window."""
        name = "tenant-clean-" + UUID(str(migration_id)).hex[:20]
        ids = [str(UUID(str(value))) for value in job_ids]
        script = ("import shutil\nfrom pathlib import Path\n"
                  f"project={str(UUID(str(project_id)))!r}\njob_ids={ids!r}\n"
                  "for relative in [f'projects/{project}', f'history/{project}.git', *[f'jobs/{j}' for j in job_ids]]:\n"
                  " path=Path('/source')/relative\n"
                  " if path.is_dir(): shutil.rmtree(path)\n"
                  " elif path.exists(): path.unlink()\n")
        claim = (self.settings.legacy_workspaces_claim if legacy_source
                 else generation_claim(source_tenant))
        security = {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                    "capabilities": {"drop": ["ALL"]}}
        pod = {"apiVersion": "v1", "kind": "Pod", "metadata": {"name": name,
            "namespace": self.settings.namespace, "labels": {"app": "koyorina-tenant-cleanup"}},
            "spec": {"restartPolicy": "Never", "automountServiceAccountToken": False,
                **self.settings.scheduling(),
                "securityContext": {"runAsNonRoot": True, "runAsUser": 10001,
                                    "runAsGroup": 10001, "fsGroup": 10001,
                                    "fsGroupChangePolicy": "OnRootMismatch",
                                    "seccompProfile": {"type": "RuntimeDefault"}},
                "containers": [{"name": "cleanup", "image": self.settings.agent_image,
                    "command": ["python", "-c", script], "securityContext": security,
                    "volumeMounts": [{"name": "source", "mountPath": "/source"},
                                     {"name": "tmp", "mountPath": "/tmp"}]}],
                "volumes": [{"name": "source", "persistentVolumeClaim": {"claimName": claim}},
                            {"name": "tmp", "emptyDir": {"sizeLimit": "64Mi"}}]}}
        if self.settings.image_pull_secret:
            pod["spec"]["imagePullSecrets"] = [{"name": self.settings.image_pull_secret}]
        if self.settings.agent_toleration:
            pod["spec"]["tolerations"] = [{"key": "workload", "operator": "Equal",
                "value": f"{self.settings.app_name}-agent", "effect": "NoSchedule"}]
        await self.kube("POST", "pods", body=pod)
        for _ in range(180):
            phase = ((await self.kube("GET", "pods", name)) or {}).get("status", {}).get("phase")
            if phase == "Succeeded":
                await self.kube("DELETE", "pods", name)
                return {"status": "cleaned"}
            if phase == "Failed":
                raise HTTPException(503, "移行元データを削除できませんでした。")
            await asyncio.sleep(2)
        raise HTTPException(503, "移行元データの削除が時間内に完了しませんでした。")

    async def prepare(self, user_id, route="codex", tenant=None, *, auth_only=False):
        if not auth_only:
            # A single-node RWO claim cannot be mounted safely by the former provider-specific
            # worker and the shared worker at once. Remove idle legacy workers on first use;
            # busy ones are left for the normal deployment/reaper path and block this start.
            await self.remove_legacy_workers(user_id, tenant)
        settings, wif = await self.effective(None if auth_only else tenant)
        wif = None if auth_only else wif
        if wif:
            await self.kube("POST", "serviceaccounts", body={
                "apiVersion": "v1", "kind": "ServiceAccount", "automountServiceAccountToken": False,
                "metadata": {"name": agent_service_account(settings), "namespace": self.settings.namespace}})
        manifests = resources(user_id, settings, route, tenant, auth_only=auth_only, wif=wif)
        name = worker_name(user_id, route, None if auth_only else tenant)
        secret = await self.kube("GET", "secrets", name)
        if secret is None:
            await self.kube("POST", "secrets", body={"apiVersion": "v1", "kind": "Secret",
                "metadata": manifests["pods"]["metadata"], "type": "Opaque",
                "stringData": {"token": secrets.token_urlsafe(48)}})
        for key, body in manifests.items():
            resource = RESOURCE_KINDS.get(key, key)
            if await self.kube("GET", resource, body["metadata"]["name"]) is None:
                await self.kube("POST", resource, body=body)

    async def effective(self, tenant=None):
        """環境の設定に、画面（システム設定）の生成AIを重ね、テナントの設定があればさらに重ねる。

        戻り値は (設定, WIF)。WIF は Vertex AI で Workload Identity 連携を使うときだけ。
        設定は ConfigMap に置く。30秒だけ覚えておき、保存されたら捨てる。
        """
        now = asyncio.get_running_loop().time()
        cached = getattr(self, "_system_gemini", None)
        if cached is None or now - cached[0] >= 30:
            names = {"gemini": system_gemini_name(self.settings),
                     **{kind: system_llm_name(self.settings, kind) for kind in SYSTEM_LLM}}
            values = {}
            for kind, name in names.items():
                try:
                    config = await self.kube("GET", "configmaps", name)
                    values[kind] = (config or {}).get("data") or {}
                except (OSError, HTTPException):
                    # 一時的に読めないだけで依頼ごと止めない。直前の値（無ければ環境の設定）で続ける。
                    values[kind] = cached[1].get(kind, {}) if cached else {}
            cached = (now, values)
            self._system_gemini = cached
        update, wif, _ = llm_overlay(cached[1], {
            "gemini": system_gemini_name(self.settings),
            **{kind: system_llm_name(self.settings, kind) for kind in SYSTEM_LLM}})
        settings = self.settings.model_copy(update=update) if update else self.settings
        if tenant is None:
            return settings, wif
        values = await self.tenant_llm_values(tenant, now)
        if not values:
            return settings, wif
        update, tenant_wif, gemini_set = llm_overlay(values, {
            **{kind: tenant_llm_name(self.settings, tenant, kind) for kind in ("gemini", *SYSTEM_LLM)},
            "config_map": tenant_llm_name(self.settings, tenant),
            "service_account": tenant_agent_service_account(self.settings, tenant)})
        # テナントが Gemini を決めていれば、WIF もテナントのもの（無ければ WIF なし）に替える。
        return settings.model_copy(update=update), (tenant_wif if gemini_set else wif)

    async def tenant_llm_values(self, tenant, now) -> dict:
        """テナントの生成AIの設定（種類ごとの辞書）。無ければ空。30秒だけ覚えておく。"""
        cache = self.__dict__.setdefault("_tenant_llm", {})
        key = canonical_tenant_id(tenant)
        cached = cache.get(key)
        if cached is None or now - cached[0] >= 30:
            try:
                config = await self.kube("GET", "configmaps", tenant_llm_name(self.settings, key))
                data = (config or {}).get("data") or {}
            except (OSError, HTTPException):
                data = cached[1] if cached else {}
            cached = (now, data)
            cache[key] = cached
        data = cached[1]
        values = {}
        for name, value in data.items():
            kind, _, field = name.partition(".")
            if field:
                values.setdefault(kind, {})[field] = value
        if "credential-config.json" in data:
            values.setdefault("gemini", {})["credential-config.json"] = data["credential-config.json"]
        return values

    async def set_tenant_llm(self, tenant, payload: TenantLlmInput):
        """テナントの生成AIを置く。ConfigMap 1つ（種類.項目）と、キーは種類ごとの Secret。"""
        tenant = canonical_tenant_id(tenant)
        namespace = self.settings.namespace
        base = tenant_llm_name(self.settings, tenant)
        await self.kube("DELETE", "configmaps", base)
        for kind in ("gemini", *SYSTEM_LLM):
            await self.kube("DELETE", "secrets", tenant_llm_name(self.settings, tenant, kind))
        data, keys = {}, {}
        if "gemini" in payload.disabled:
            data["gemini.disabled"] = "true"
        elif payload.gemini is not None and payload.gemini.backend:
            gemini = payload.gemini
            data.update({"gemini.backend": gemini.backend, "gemini.model": gemini.model,
                         "gemini.project": gemini.project, "gemini.location": gemini.location,
                         "gemini.audience": gemini.audience})
            if gemini.audience:
                data["credential-config.json"] = gemini.config
            if gemini.backend == "developer":
                keys["gemini"] = ("GEMINI_API_KEY", gemini.api_key.get_secret_value())
        for kind, field in (("antigravity", "antigravity"), ("openai-compatible", "openai_compatible"),
                            ("claude", "claude")):
            part = getattr(payload, field)
            if field in payload.disabled:
                data[f"{kind}.enabled"] = "false"
            elif part is not None and part.enabled is not None:
                for name, value in part.model_dump(exclude={"api_key"}).items():
                    data[f"{kind}.{name}"] = str(value).lower() if isinstance(value, bool) else str(value)
                key = part.api_key.get_secret_value()
                data[f"{kind}.has_key"] = "true" if key else "false"
                if key:
                    keys[kind] = (SYSTEM_LLM[kind][1], key)
        if data:
            await self.kube("POST", "configmaps", body={
                "apiVersion": "v1", "kind": "ConfigMap",
                "metadata": {"name": base, "namespace": namespace}, "data": data})
        for kind, (name, key) in keys.items():
            await self.kube("POST", "secrets", body={
                "apiVersion": "v1", "kind": "Secret", "type": "Opaque",
                "metadata": {"name": tenant_llm_name(self.settings, tenant, kind), "namespace": namespace},
                "stringData": {name: key}})
        self.__dict__.setdefault("_tenant_llm", {}).pop(tenant, None)
        self.retire_stale_soon()
        return {"tenant": tenant, "kinds": sorted({name.partition(".")[0] for name in data})}

    async def set_system_llm(self, kind: str, payload):
        """画面で保存した Antigravity／OpenAI 互換 API を置く。enabled が None なら消す。"""
        name = system_llm_name(self.settings, kind)
        await self.kube("DELETE", "configmaps", name)
        await self.kube("DELETE", "secrets", name)
        if payload.enabled is not None:
            data = {key: str(value).lower() if isinstance(value, bool) else str(value)
                    for key, value in payload.model_dump(exclude={"api_key"}).items()}
            key = payload.api_key.get_secret_value()
            data["has_key"] = "true" if key else "false"
            await self.kube("POST", "configmaps", body={
                "apiVersion": "v1", "kind": "ConfigMap",
                "metadata": {"name": name, "namespace": self.settings.namespace}, "data": data})
            if key:
                await self.kube("POST", "secrets", body={
                    "apiVersion": "v1", "kind": "Secret", "type": "Opaque",
                    "metadata": {"name": name, "namespace": self.settings.namespace},
                    "stringData": {SYSTEM_LLM[kind][1]: key}})
        self._system_gemini = None
        self.retire_stale_soon()
        return {"kind": kind, "enabled": payload.enabled}

    async def set_system_gemini(self, payload: "SystemGeminiInput"):
        """本体から受け取った設定を保存する。backend が空なら消す（環境の設定へ戻る）。"""
        name = system_gemini_name(self.settings)
        await self.kube("DELETE", "configmaps", name)
        await self.kube("DELETE", "secrets", name)
        if payload.backend:
            await self.kube("POST", "configmaps", body={
                "apiVersion": "v1", "kind": "ConfigMap",
                "metadata": {"name": name, "namespace": self.settings.namespace},
                "data": {"backend": payload.backend, "model": payload.model, "project": payload.project,
                         "location": payload.location, "audience": payload.audience,
                         "credential-config.json": payload.config}})
            if payload.backend == "developer":
                await self.kube("POST", "secrets", body={
                    "apiVersion": "v1", "kind": "Secret", "type": "Opaque",
                    "metadata": {"name": name, "namespace": self.settings.namespace},
                    "stringData": {"GEMINI_API_KEY": payload.api_key.get_secret_value()}})
        self._system_gemini = None
        self.retire_stale_soon()
        return {"backend": payload.backend or "env", "wif": bool(payload.audience),
                "subject": f"system:serviceaccount:{self.settings.namespace}:{agent_service_account(self.settings)}"}

    async def remove_legacy_workers(self, user_id, tenant):
        """Retire idle provider-specific workers before creating the shared worker."""
        tenant = canonical_tenant_id(tenant)
        for route in LEGACY_ROUTES:
            name = legacy_generation_worker(route, user_id, tenant)
            pod = await self.kube("GET", "pods", name)
            if pod is None:
                continue
            if not ready(pod):
                raise HTTPException(503, "以前の生成環境を終了しています。少し待って再度お試しください。")
            try:
                state = await self.relay(user_id, "GET", "/runtime", route=route,
                                         tenant=tenant, legacy=True)
            except HTTPException:
                raise HTTPException(503, "以前の生成環境を確認しています。少し待って再度お試しください。") from None
            if state.get("busy"):
                raise HTTPException(409, "以前の生成処理が進行中です。完了後に再度お試しください。")
            await self.remove_worker_endpoint(name)

    async def remove_worker_endpoint(self, name, *, remove_pod=True):
        """Remove the disposable network endpoint together with its worker Pod.

        The credential secret and PVC are intentionally retained.  A Service without a Pod has
        no purpose, and retaining one for every worker that has ever existed makes the namespace
        look occupied even after idle Pods have been reaped.
        """
        if remove_pod and await self.kube("GET", "pods", name) is not None:
            await self.kube("DELETE", "pods", name)
        if await self.kube("GET", "services", name) is not None:
            await self.kube("DELETE", "services", name)

    async def reap_orphan_services(self, grace_seconds=300):
        """Delete old agent Services that no longer select a same-named Pod.

        A short grace period avoids racing with ``prepare()``, which creates the Service shortly
        before its Pod.  This also removes Services left by the pre-tenant worker naming scheme.
        """
        selector = f"?labelSelector=app={self.settings.agent_label}"
        pods = await self.kube("GET", "pods", query=selector)
        services = await self.kube("GET", "services", query=selector)
        pod_names = {item["metadata"]["name"] for item in (pods or {}).get("items", [])}
        now = datetime.now(timezone.utc)
        removed = []
        for service in (services or {}).get("items", []):
            metadata = service.get("metadata") or {}
            name, created = metadata.get("name"), metadata.get("creationTimestamp")
            if not name or name in pod_names or not created:
                continue
            try:
                age = (now - datetime.fromisoformat(created.replace("Z", "+00:00"))).total_seconds()
            except ValueError:
                continue
            if age < grace_seconds:
                continue
            await self.kube("DELETE", "services", name)
            removed.append(name)
        return removed

    async def revoke_support(self, user_id, tenant):
        """Remove support worker Pods without touching credentials or tenant data."""
        removed = []
        names = [worker_name(user_id, "shared", tenant),
                 *(legacy_generation_worker(route, user_id, tenant) for route in LEGACY_ROUTES)]
        for name in names:
            if await self.kube("GET", "pods", name) is not None:
                await self.remove_worker_endpoint(name)
                removed.append(name)
            elif await self.kube("GET", "services", name) is not None:
                await self.remove_worker_endpoint(name, remove_pod=False)
        return {"status": "revoked", "pods": removed}

    async def runtime_status(self, user_id):
        """Summarize this user's generation Pods without exposing other users or tenant data."""
        identity = str(UUID(str(user_id)))
        listed = await self.kube("GET", "pods", query=
                                 f"?labelSelector=app={self.settings.agent_label},forge-user={identity}")
        result = {route: {"state": "stopped", "pods": 0, "running": 0,
                          "starting": 0, "errors": 0} for route in ROUTES}
        for pod in (listed or {}).get("items", []):
            labels = (pod.get("metadata") or {}).get("labels") or {}
            route = labels.get("forge-route")
            # Codexのログイン用Podは生成環境ではないので、アプリバーへ数えない。
            if route not in {*ROUTES, "shared"} or labels.get("forge-purpose") == "authentication" \
                    or not labels.get("forge-tenant"):
                continue
            status = pod.get("status") or {}
            phase = status.get("phase")
            waiting = [state.get("waiting", {}).get("reason") for state in
                       status.get("containerStatuses") or []]
            fatal = any(reason in {"CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull",
                                   "CreateContainerError", "RunContainerError"} for reason in waiting)
            # A shared worker makes both providers available. Legacy workers are still
            # reported under their provider while a rolling migration is in progress.
            buckets = result.values() if route == "shared" else (result[route],)
            for bucket in buckets:
                bucket["pods"] += 1
                if phase == "Failed" or fatal:
                    bucket["errors"] += 1
                elif ready(pod):
                    bucket["running"] += 1
                else:
                    bucket["starting"] += 1
        for bucket in result.values():
            bucket["state"] = ("error" if bucket["errors"] else
                               "starting" if bucket["starting"] else
                               "running" if bucket["running"] else "stopped")
        return result

    async def delete_legacy_claim(self):
        await self.kube("DELETE", "persistentvolumeclaims",
                        self.settings.legacy_workspaces_claim)
        return {"status": "deleted"}

    async def wait_and_start(self, user_id, tenant, payload, route):
        """Podを用意し、Readyになってから依頼を一度だけ渡す。"""
        await self.prepare(user_id, route, tenant)
        name = worker_name(user_id, route, tenant)
        for _ in range(90):
            pod = await self.kube("GET", "pods", name)
            if pod is not None and ready(pod):
                return await self.relay(user_id, "POST", "/jobs", payload, route=route, tenant=tenant)
            await asyncio.sleep(2)
        raise HTTPException(503, "専用の実行環境を準備できませんでした。しばらく待って再度お試しください。")

    async def wait_and_relay(self, user_id, tenant, method, path, body, route):
        """Prepare an interview worker and keep the first click alive during its short startup."""
        await self.prepare(user_id, route, tenant)
        name = worker_name(user_id, route, tenant)
        for _ in range(25):
            pod = await self.kube("GET", "pods", name)
            if pod is not None and ready(pod):
                return await self.relay(user_id, method, path, body, route=route, tenant=tenant)
            await asyncio.sleep(2)
        raise HTTPException(503, "専用の実行環境を準備できませんでした。しばらく待って再度お試しください。")

    def retire_stale_soon(self):
        """設定を保存した直後に、古い設定のまま使われていない Pod を入れ替える。

        Pod ごとに状態を問い合わせるので時間がかかる。保存の応答は待たせない。
        生成中のものは、定期の見回り（reap_idle）が終わりしだい入れ替える。
        """
        async def run():
            try:
                for name in await self.reap_idle():
                    logger.info("replaced_agent_for_new_llm_settings %s", name)
            except Exception as error:  # 見回りは次の周回でもう一度行う
                logger.warning("replace_agents_failed %s: %s", type(error).__name__, str(error)[:300])
        task = asyncio.create_task(run())
        self._retire_tasks = getattr(self, "_retire_tasks", set())
        self._retire_tasks.add(task)
        task.add_done_callback(self._retire_tasks.discard)

    async def reap_idle(self):
        """使われていない生成Podを片付ける。消しても失うものは無い。

        認証は利用者PVC、コードとジョブと履歴はテナントPVCにあるので、次に使うときに
        作り直せば同じ続きから始まる（`relay` が「Podが無くPVCがある」を既に扱う）。

        消さないのは、生成中・ヒアリング中・ログイン手続き中。配備での入れ替えと
        同じ判断を使う。状態の問い合わせは「使った」に数えない（画面を開いたまま
        席を外した人のPodが永久に残るため。数え方はagent側のIDLE_IGNORED）。

        戻り値は消したPodの名前。呼び出し側が記録に残す。
        """
        limit = self.settings.idle_minutes * 60
        fingerprints = {}
        listing = await self.kube("GET", "pods", query=f"?labelSelector=app={self.settings.agent_label}")
        removed = []
        for pod in (listing or {}).get("items", []):
            name = pod["metadata"]["name"]
            labels = pod["metadata"].get("labels") or {}
            user, route = labels.get("forge-user"), labels.get("forge-route")
            tenant = labels.get("forge-tenant")
            if not user or route not in {*ROUTES, "shared"}:
                continue  # 誰のものか分からないPodは触らない。
            if not ready(pod):
                continue  # 起動中かもしれない。待つ。
            try:
                state = await self.relay(UUID(user), "GET", "/runtime", route=route,
                                         tenant=tenant, auth_only=not tenant,
                                         legacy=route in LEGACY_ROUTES and bool(tenant))
            except (HTTPException, ValueError):
                continue  # 状態を確かめられないものは消さない。
            # 生成AIの設定（システム設定）が変わった Pod は、使っていなければすぐ入れ替える。
            # 生成中なら待ち、終わった次の見回りで入れ替える。
            # 指紋はテナントの実効設定（システム＋テナント）で比べる。
            if tenant and tenant not in fingerprints:
                fingerprints[tenant] = llm_fingerprint(*(await self.effective(tenant)))
            stale = bool(tenant) and (pod["metadata"].get("annotations") or {}).get(LLM_ANNOTATION) != fingerprints[tenant]
            if state.get("busy") or not (stale or (limit > 0 and state.get("idle_seconds", 0) >= limit)):
                continue
            await self.remove_worker_endpoint(name)
            removed.append(name)
        return removed

    async def relay(self, user_id, method, path, body=None, prepare=False, route=None,
                    tenant=None, *, auth_only=False, legacy=False):
        if tenant is None and path in {"/account", "/login", "/logout", "/models"}:
            auth_only = True
        if tenant is None and not auth_only:
            tenant = "00000000-0000-4000-8000-000000000001"
        if not auth_only:
            tenant = canonical_tenant_id(tenant)
        route = route or ("codex" if auth_only else "shared")
        name = (legacy_generation_worker(route, user_id, tenant) if legacy and not auth_only
                else worker_name(user_id, route, None if auth_only else tenant))
        if prepare:
            await self.prepare(user_id, route, tenant, auth_only=auth_only)
        waiting = {"status": "preparing", "email": None, "plan": None, "login": None,
                   "error": None, "busy": False}
        pod = await self.kube("GET", "pods", name)
        if pod is None:
            # 保存領域が残っていれば、その人はもう使ったことがある。接続情報もそこにある。
            # 未接続として扱うと、実際は繋がっているのにログインを求めることになる。
            auth_claim = f"codex-{UUID(str(user_id)).hex}"
            if auth_only and await self.kube("GET", "persistentvolumeclaims", auth_claim) is not None:
                await self.prepare(user_id, route, tenant, auth_only=auth_only)
                if path in {"/account", "/login"}:
                    return waiting
            elif (not auth_only and await self.kube(
                    "GET", "persistentvolumeclaims", generation_claim(tenant)) is not None):
                await self.prepare(user_id, route, tenant)
            elif path == "/account":
                return {"status": "disconnected", "email": None, "plan": None, "login": None, "error": None, "busy": False}
            raise HTTPException(503, "AppGenの実行環境を準備しています。少し待って再実行してください。")
        if not ready(pod):
            # ログインも「準備中」を返す。押した操作が赤いエラーで返ると、失敗に見える。
            if path in {"/account", "/login"}:
                return waiting
            raise HTTPException(503, "専用の実行環境を準備しています。数十秒待って再度お試しください。")
        # The address comes from the trusted Kubernetes API, never from a request parameter.
        # Reading it for every relay also follows a recreated Pod without keeping one Service
        # object per user/tenant/provider combination.
        pod_ip = pod.get("status", {}).get("podIP")
        if not pod_ip:
            raise HTTPException(503, "専用の実行環境を準備しています。数十秒待って再度お試しください。")
        secret = await self.kube("GET", "secrets", name)
        token = base64.b64decode(secret["data"]["token"]).decode()
        async with httpx.AsyncClient(timeout=40, trust_env=False) as client:
            response = await client.request(method, f"http://{pod_ip}:8080{path}",
                headers={"Authorization": "Bearer " + token}, json=body)
        if not response.is_success:
            if response.status_code == 409 and re.fullmatch(r"/jobs/[0-9a-f-]{36}/bundle", path):
                try:
                    value = response.json()
                    message = value.get("error") or value.get("detail")
                except (ValueError, AttributeError):
                    message = None
                if isinstance(message, str) and 0 < len(message) <= 600:
                    raise HTTPException(409, message)
                raise HTTPException(409, "保存済みの生成ソースが現在の検査条件に適合しません。")
            # Only fixed status messages, never raw worker/RPC exception text.
            messages = {409: "選択したAIの接続状態と実行中の処理を確認してください。", 404: "生成履歴が見つかりません。"}
            raise HTTPException(response.status_code if response.status_code in messages else 503,
                                messages.get(response.status_code, "AIの接続状態を確認して再度お試しください。"))
        return response.json()


COMPOSE_UNSUPPORTED = "Docker Compose版では使えません。本番構成（k3s）で利用してください。"
COMPOSE_NO_VERTEX = ("Docker Compose版では Vertex AI（ADC・Workload Identity）を使えません。"
                     "Gemini API（Google AI StudioのAPIキー）を選んでください。")
# composeが常駐させるエージェントの身元。環境の値（compose.yaml）をそのまま使い、
# 画面の設定で上書きさせない。
COMPOSE_FIXED_AGENT_ENV = frozenset({"AGENT_USER_ID", "AGENT_TOKEN", "AGENT_TENANT_ID",
                                     "AGENT_POD_NAME", "AGENT_NAMESPACE", "AGENT_ENVIRONMENT"})
COMPOSE_USER = UUID("00000000-0000-4000-8000-00000000c0de")
COMPOSE_TENANT = "00000000-0000-4000-8000-000000000001"


class ComposeProvisioner(Provisioner):
    """Docker Composeのお試し版。k8s APIの代わりに、常駐エージェント1つを返す。

    生成の流れ（prepare → Readyを待つ → relay）と、画面の生成AI設定の保存・重ね合わせ
    （set_system_* / effective）は、k8s版と同じコードをそのまま通す。違うのは kube() だけ:

    - Pod: いつもReadyの1つ。アドレスは agent_url のホスト、トークンは AGENT_TOKEN
    - ConfigMap / Secret: 設定置き場のファイル（settings_dir/store.json）に読み書きする
    - それ以外（PVC・Service等）: 在るものとして扱い、作る・消すは何もしない

    k8s版は設定が変わると新しい環境変数でPodを作り直す。ここでは同じ環境変数を
    settings_dir/agent-env.json に書き、エージェントの起動役（compose_agent）が
    手すきになったところで読み直して再起動する。1人用・1テナント前提。
    """

    def __init__(self, settings):
        super().__init__(settings)
        self.agent_host = urlparse(settings.agent_url).hostname
        self.settings_dir = Path(settings.settings_dir)

    # ── 設定置き場（ConfigMap/Secretの代わり） ─────────────────────────
    def store(self) -> dict:
        path = self.settings_dir / "store.json"
        return json.loads(path.read_text()) if path.is_file() else {"configmaps": {}, "secrets": {}}

    def write_private(self, name: str, document: dict):
        """APIキーを含む。エージェントとcontroller（uid 10001）だけが読めるようにする。"""
        self.settings_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = self.settings_dir / (name + ".tmp")
        temporary.write_text(json.dumps(document, ensure_ascii=False, sort_keys=True))
        temporary.chmod(0o600)
        temporary.replace(self.settings_dir / name)  # 読み手が書きかけを見ないように置き換える

    async def kube(self, method, resource, name="", body=None, query="", text=False):
        if resource in {"configmaps", "secrets"}:
            store = self.store()
            items = store.setdefault(resource, {})
            if method == "GET" and name in items:
                return {"metadata": {"name": name}, "data": items[name]}
            if method == "POST":
                name = body["metadata"]["name"]
                data = dict(body.get("data") or {})
                for key, value in (body.get("stringData") or {}).items():
                    # 読む側（k8sと同じ）はSecretの値をbase64で受け取る。
                    data[key] = base64.b64encode(value.encode()).decode() if resource == "secrets" else value
                items[name] = data
                self.write_private("store.json", store)
                return {"metadata": {"name": name}, "data": data}
            if method == "DELETE":
                if items.pop(name, None) is not None:
                    self.write_private("store.json", store)
                return {}
            if resource == "secrets" and method == "GET":
                # ワーカーの接続鍵。composeが渡した AGENT_TOKEN を返す。
                token = self.settings.agent_token.get_secret_value()
                return {"data": {"token": base64.b64encode(token.encode()).decode()}}
            return None
        if method != "GET":
            return {}
        if resource == "pods":
            if not name:
                return {"items": []}  # 一覧（回収・状態集計）は空。片付けるものは無い。
            return {"metadata": {"name": name, "labels": {}},
                    "status": {"phase": "Running", "podIP": self.agent_host,
                               "conditions": [{"type": "Ready", "status": "True"}]}}
        return {"items": []} if not name else {"metadata": {"name": name}}

    # ── 設定をエージェントへ届ける ─────────────────────────────────
    async def agent_environment(self) -> dict[str, str]:
        """k8s版なら生成Podに付ける環境変数を、そのまま値に解いて返す。"""
        settings, _ = await self.effective(COMPOSE_TENANT)
        pod = resources(COMPOSE_USER, settings, None, COMPOSE_TENANT)["pods"]
        secrets_store = self.store()["secrets"]
        environment = {}
        for item in pod["spec"]["containers"][0]["env"]:
            name = item["name"]
            if name in COMPOSE_FIXED_AGENT_ENV:
                continue
            if "value" in item:
                environment[name] = str(item["value"])
                continue
            reference = (item.get("valueFrom") or {}).get("secretKeyRef")
            stored = secrets_store.get((reference or {}).get("name"), {}).get((reference or {}).get("key"))
            if stored is not None:
                environment[name] = base64.b64decode(stored).decode()
            # 画面で入れていない鍵（compose.env・secrets由来）は書かない。起動時の値が残る。
        return environment

    async def publish_agent_environment(self):
        self.write_private("agent-env.json", await self.agent_environment())

    def retire_stale_soon(self):
        """k8s版はPodを入れ替える。ここでは次の環境を書き、入れ替えはエージェント側に任せる。"""
        async def run():
            try:
                await self.publish_agent_environment()
                logger.info("compose_agent_environment_updated")
            except Exception as error:
                logger.warning("compose_agent_environment_failed %s: %s",
                               type(error).__name__, str(error)[:300])
        task = asyncio.create_task(run())
        self._retire_tasks = getattr(self, "_retire_tasks", set())
        self._retire_tasks.add(task)
        task.add_done_callback(self._retire_tasks.discard)

    async def set_system_gemini(self, payload):
        if payload.backend == "vertex":
            raise HTTPException(409, COMPOSE_NO_VERTEX)
        return await super().set_system_gemini(payload)

    async def set_tenant_llm(self, tenant, payload):
        gemini = getattr(payload, "gemini", None)
        if gemini is not None and getattr(gemini, "backend", "") == "vertex":
            raise HTTPException(409, COMPOSE_NO_VERTEX)
        return await super().set_tenant_llm(tenant, payload)

    # ── k8s固有の操作 ───────────────────────────────────────────
    async def prepare(self, user_id, route="codex", tenant=None, *, auth_only=False):
        return None  # エージェントはcomposeが起動済み。作るものは無い。

    async def remove_legacy_workers(self, user_id, tenant):
        return None  # 旧来のプロバイダ別ワーカーはcompose版に存在しない。

    async def runtime_status(self, user_id):
        try:
            async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
                response = await client.get(f"{self.settings.agent_url}/healthz")
            up = response.is_success
        except httpx.HTTPError:
            up = False
        state = {"state": "running" if up else "starting", "pods": 1,
                 "running": int(up), "starting": int(not up), "errors": 0}
        return {route: dict(state) for route in ROUTES}

    async def unsupported(self, *args, **kwargs):
        raise HTTPException(409, COMPOSE_UNSUPPORTED)

    measure_storage = migrate = cleanup_migration = delete_legacy_claim = unsupported

    async def reap_idle(self):
        return []

    async def reap_orphan_services(self, grace_seconds=300):
        return []


def create_controller(settings=None):
    configure_logging()
    settings = settings or ControllerSettings()
    provisioner = (ComposeProvisioner if settings.backend == "compose" else Provisioner)(settings)

    async def sweep():
        """使われていないPodを定期的に片付ける。失敗しても回り続けること。

        ここで例外を外へ出すとタスクが死に、以後ずっと回収されなくなる。
        止まったことにも気づけないので、記録して次の周回へ進む。
        """
        while True:
            await asyncio.sleep(settings.idle_sweep_seconds)
            try:
                # 使われていない Pod と、生成AIの設定が古い Pod を片付ける（idle_minutes=0 でも後者は行う）。
                for name in await provisioner.reap_idle():
                    logger.info("reaped_idle_agent %s", name)
                for name in await provisioner.reap_orphan_services():
                    logger.info("reaped_orphan_agent_service %s", name)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                # 型名だけだと原因に辿り着けない（実際、kube() の引数違いで
                # 毎回失敗していたのに TypeError としか出ていなかった）。
                # これは運用のログで、利用者へ返す文面ではない。
                logger.warning("idle_sweep_failed %s: %s",
                               type(error).__name__, str(error)[:300])

    @asynccontextmanager
    async def lifespan(_):
        # Orphan Service cleanup remains useful even when Pod idle reaping is disabled.
        task = asyncio.create_task(sweep())
        try:
            yield
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.provisioner = provisioner
    # Podの起動待ちはHTTP応答から切り離す。画面には先に履歴を返し、待機中でも停止可能にする。
    pending_jobs = {}
    app.state.pending_jobs = pending_jobs
    # K8s handles creation races, but serialize login/job mutations per controller.
    mutation_lock = asyncio.Lock()

    @app.middleware("http")
    async def authenticate(request, call_next):
        if request.url.path != "/healthz" and not secrets.compare_digest(
                request.headers.get("authorization", ""), "Bearer " + settings.token.get_secret_value()):
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(Exception)
    async def safe_error(request, exc):
        return JSONResponse({"error": "AppGenの実行環境に接続できません。"}, status_code=503)

    @app.get("/healthz")
    async def health():
        return {"status": "ok"}

    @app.post("/migrations")
    async def migrate(payload: MigrationInput):
        async with mutation_lock:
            return await provisioner.migrate(payload.migration_id, payload.project_id,
                payload.job_ids, payload.source_tenant_id, payload.target_tenant_id,
                legacy_source=payload.legacy_source)

    @app.post("/migrations/cleanup")
    async def cleanup_migration(payload: MigrationInput):
        async with mutation_lock:
            return await provisioner.cleanup_migration(
                payload.migration_id, payload.project_id, payload.job_ids,
                payload.source_tenant_id, legacy_source=payload.legacy_source)

    @app.delete("/migrations/legacy-claim")
    async def delete_legacy_claim():
        async with mutation_lock:
            return await provisioner.delete_legacy_claim()

    @app.get("/tenants/{tenant_id}/storage")
    async def storage(tenant_id: UUID):
        async with mutation_lock:
            return await provisioner.measure_storage(tenant_id)

    @app.put("/settings/tenants/{tenant_id}/llm")
    async def tenant_llm(tenant_id: UUID, payload: TenantLlmInput):
        async with mutation_lock:
            return await provisioner.set_tenant_llm(tenant_id, payload)

    @app.put("/settings/antigravity")
    async def system_antigravity(payload: SystemAntigravityInput):
        async with mutation_lock:
            return await provisioner.set_system_llm("antigravity", payload)

    @app.put("/settings/claude")
    async def system_claude(payload: SystemClaudeInput):
        async with mutation_lock:
            return await provisioner.set_system_llm("claude", payload)

    @app.put("/settings/openai-compatible")
    async def system_openai_compatible(payload: SystemOpenAICompatibleInput):
        async with mutation_lock:
            return await provisioner.set_system_llm("openai-compatible", payload)

    @app.put("/settings/gemini")
    async def system_gemini(payload: SystemGeminiInput):
        async with mutation_lock:
            return await provisioner.set_system_gemini(payload)

    @app.post("/support/revoke")
    async def revoke_support(payload: SupportRevokeInput):
        async with mutation_lock:
            return await provisioner.revoke_support(payload.actor_id, payload.tenant_id)

    @app.get("/users/{user_id}/account")
    async def account(user_id: UUID):
        result = await provisioner.relay(user_id, "GET", "/account", auth_only=True)
        return {**result, "generator": settings.generator}

    @app.get("/users/{user_id}/runtimes")
    async def runtimes(user_id: UUID):
        result = await provisioner.runtime_status(user_id)
        # Pod作成より先にジョブ受付を返すため、その短い間も起動待ちとして見せる。
        for (tenant, owner, job), (_, route) in pending_jobs.items():
            if owner == user_id and result[route]["state"] == "stopped":
                result[route]["state"] = "starting"
                result[route]["starting"] = 1
        return result

    @app.post("/tenants/{tenant_id}/users/{user_id}/runtime/start", status_code=202)
    async def warm_runtime(tenant_id: UUID, user_id: UUID):
        """Start the shared generation worker after Koyorina login/tenant selection."""
        async with mutation_lock:
            await provisioner.prepare(user_id, settings.generator, str(tenant_id))
        return {"status": "starting"}

    @app.post("/users/{user_id}/{action}")
    async def action(user_id: UUID, action: Literal["login", "logout"]):
        async with mutation_lock:
            # 他の中継と同じ書き方に揃える。連結だと、送り先の一覧を機械で
            # 読み取れず、口が無くなっても気づけない（test_relay_paths）。
            return await provisioner.relay(user_id, "POST", f"/{action}",
                                           prepare=action == "login", auth_only=True)

    @app.post("/tenants/{tenant_id}/users/{user_id}/jobs/start")
    async def generate(tenant_id: UUID, user_id: UUID, payload: GenerationInput):
        async with mutation_lock:
            route = worker_route((await provisioner.effective(tenant_id))[0], payload.generator)
            key = (tenant_id, user_id, payload.job_id)
            if key not in pending_jobs:
                task = asyncio.create_task(provisioner.wait_and_start(
                    user_id, str(tenant_id), payload.model_dump(mode="json"), route))
                pending_jobs[key] = (task, route)

                def finish(completed, *, job_key=key):
                    current = pending_jobs.get(job_key)
                    if current is not None and current[0] is completed:
                        pending_jobs.pop(job_key, None)
                    # 例外を回収する。DB側は状態確認で開始失敗として確定する。
                    with suppress(asyncio.CancelledError, Exception):
                        completed.result()

                task.add_done_callback(finish)
            return {"status": "starting", "generator": route}

    @app.get("/tenants/{tenant_id}/users/{user_id}/projects/{project_id}/attachments")
    async def attachment_list(tenant_id: UUID, user_id: UUID, project_id: UUID):
        # Opening a specification mounts the chat panel, which asks for this list. Do not
        # create a worker solely for that read; login warm-up owns the startup timing.
        pod = await provisioner.kube("GET", "pods", worker_name(user_id, "shared", tenant_id))
        if pod is None:
            return {"attachments": []}
        if not ready(pod):
            return {"attachments": []}
        return await provisioner.relay(user_id, "GET", f"/projects/{project_id}/attachments",
                                       tenant=tenant_id)

    @app.get("/tenants/{tenant_id}/users/{user_id}/projects/{project_id}/interview")
    async def interview_status(tenant_id: UUID, user_id: UUID, project_id: UUID,
                               provider: Literal["codex", "gemini"] = "codex"):
        route = worker_route((await provisioner.effective(tenant_id))[0], provider)
        pod = await provisioner.kube("GET", "pods", worker_name(user_id, "shared", tenant_id))
        if pod is None:
            return {"status": "idle", "provider": route, "question": None,
                    "questions": [], "result": None, "error": None}
        if not ready(pod):
            return {"status": "starting", "provider": route, "question": None,
                    "questions": [], "result": None, "error": None}
        return await provisioner.relay(user_id, "GET",
                                       f"/projects/{project_id}/interview?provider={route}",
                                       route=route, tenant=tenant_id)

    @app.post("/tenants/{tenant_id}/users/{user_id}/projects/{project_id}/interview")
    async def interview_start(tenant_id: UUID, user_id: UUID, project_id: UUID, payload: ProjectInput,
                              provider: Literal["codex", "gemini"] = "codex"):
        async with mutation_lock:
            route = worker_route((await provisioner.effective(tenant_id))[0], provider)
            return await provisioner.wait_and_relay(
                user_id, str(tenant_id), "POST",
                f"/projects/{project_id}/interview?provider={route}",
                payload.model_dump(mode="json"), route)

    @app.post("/tenants/{tenant_id}/users/{user_id}/projects/{project_id}/interview/{action}")
    async def interview_action(tenant_id: UUID, user_id: UUID, project_id: UUID,
                               action: Literal["answer", "cancel"], payload: dict | None = None,
                               provider: Literal["codex", "gemini"] = "codex"):
        async with mutation_lock:
            route = worker_route((await provisioner.effective(tenant_id))[0], provider)
            return await provisioner.relay(
                user_id, "POST", f"/projects/{project_id}/interview/{action}?provider={route}",
                payload or {}, route=route, tenant=tenant_id)

    @app.post("/tenants/{tenant_id}/users/{user_id}/projects/{project_id}/attachments")
    async def attachment_add(tenant_id: UUID, user_id: UUID, project_id: UUID, payload: dict):
        return await provisioner.wait_and_relay(
            user_id, str(tenant_id), "POST", f"/projects/{project_id}/attachments",
            payload, settings.generator)

    @app.post("/tenants/{tenant_id}/users/{user_id}/projects/{project_id}/attachments/remove")
    async def attachment_remove(tenant_id: UUID, user_id: UUID, project_id: UUID, payload: dict):
        return await provisioner.wait_and_relay(
            user_id, str(tenant_id), "POST", f"/projects/{project_id}/attachments/remove",
            payload, settings.generator)

    @app.get("/tenants/{tenant_id}/users/{user_id}/projects/{project_id}/files")
    async def files(tenant_id: UUID, user_id: UUID, project_id: UUID, path: str = ""):
        query = "?path=" + quote(path, safe="") if path else ""
        return await provisioner.relay(user_id, "GET", f"/projects/{project_id}/files{query}", tenant=tenant_id)

    @app.get("/tenants/{tenant_id}/users/{user_id}/projects/{project_id}/archive")
    async def archive(tenant_id: UUID, user_id: UUID, project_id: UUID):
        return await provisioner.relay(user_id, "GET", f"/projects/{project_id}/archive", tenant=tenant_id)

    # 変更履歴。実体はアプリ単位の共有領域にあるので、動いているワーカーなら
    # どれでも読める。ここが無いまま画面とエージェントだけ作ってあり、
    # 管理側の要求が404で返っていた（画面には「まだ記録がありません」と出る）。
    @app.get("/tenants/{tenant_id}/users/{user_id}/projects/{project_id}/history")
    async def history(tenant_id: UUID, user_id: UUID, project_id: UUID, limit: int = 50):
        return await provisioner.relay(user_id, "GET",
                                       f"/projects/{project_id}/history?limit={int(limit)}",
                                       tenant=tenant_id)

    @app.get("/tenants/{tenant_id}/users/{user_id}/projects/{project_id}/history/{commit}")
    async def history_diff(tenant_id: UUID, user_id: UUID, project_id: UUID, commit: str):
        return await provisioner.relay(user_id, "GET",
                                       f"/projects/{project_id}/history/{quote(commit, safe='')}",
                                       tenant=tenant_id)

    @app.post("/tenants/{tenant_id}/users/{user_id}/projects/{project_id}/history/{commit}/restore")
    async def history_restore(tenant_id: UUID, user_id: UUID, project_id: UUID, commit: str,
                              payload: dict | None = None):
        return await provisioner.relay(
            user_id, "POST", f"/projects/{project_id}/history/{quote(commit, safe='')}/restore",
            payload or {}, tenant=tenant_id)

    @app.delete("/tenants/{tenant_id}/users/{user_id}/projects/{project_id}/history")
    async def history_discard(tenant_id: UUID, user_id: UUID, project_id: UUID):
        return await provisioner.relay(user_id, "DELETE", f"/projects/{project_id}/history",
                                       tenant=tenant_id)

    @app.post("/tenants/{tenant_id}/users/{user_id}/projects/{project_id}/remove")
    async def remove_project(tenant_id: UUID, user_id: UUID, project_id: UUID, payload: dict | None = None):
        return await provisioner.relay(user_id, "POST", f"/projects/{project_id}/remove",
                                       payload or {}, tenant=tenant_id)

    @app.get("/users/{user_id}/models")
    async def models(user_id: UUID):
        return await provisioner.relay(user_id, "GET", "/models", auth_only=True)

    @app.post("/tenants/{tenant_id}/users/{user_id}/jobs/{job_id}/cancel")
    async def cancel(tenant_id: UUID, user_id: UUID, job_id: UUID):
        key = (tenant_id, user_id, job_id)
        async with mutation_lock:
            pending = pending_jobs.pop(key, None)
            if pending is not None:
                task, route = pending
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
                # Ready直後に依頼が届いた競合も止める。未到達なら失敗を無視してよい。
                with suppress(HTTPException):
                    await provisioner.relay(user_id, "POST", f"/jobs/{job_id}/cancel",
                                            route=route, tenant=tenant_id)
                return {"status": "failed", "failure_code": "cancelled"}
        return await provisioner.relay(user_id, "POST", f"/jobs/{job_id}/cancel", tenant=tenant_id)

    @app.post("/tenants/{tenant_id}/users/{user_id}/jobs/{job_id}/revalidate")
    async def revalidate(tenant_id: UUID, user_id: UUID, job_id: UUID, payload: dict):
        return await provisioner.relay(user_id, "POST", f"/jobs/{job_id}/revalidate", payload,
                                       tenant=tenant_id)

    @app.get("/tenants/{tenant_id}/users/{user_id}/jobs/{job_id}")
    async def job(tenant_id: UUID, user_id: UUID, job_id: UUID):
        return await provisioner.relay(user_id, "GET", f"/jobs/{job_id}", tenant=tenant_id)

    @app.get("/tenants/{tenant_id}/users/{user_id}/jobs/{job_id}/bundle")
    async def bundle(tenant_id: UUID, user_id: UUID, job_id: UUID):
        return await provisioner.relay(user_id, "GET", f"/jobs/{job_id}/bundle", tenant=tenant_id)

    @app.get("/tenants/{tenant_id}/users/{user_id}/jobs/{job_id}/progress")
    async def progress(tenant_id: UUID, user_id: UUID, job_id: UUID):
        return await provisioner.relay(user_id, "GET", f"/jobs/{job_id}/progress", tenant=tenant_id)

    # 一番外側に置く。認証で断った呼び出しも1行残す。
    app.add_middleware(RequestLogMiddleware)
    return app
