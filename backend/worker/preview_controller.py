"""プレビュー実行環境の最小権限プロビジョナ。

専用namespaceで、プロジェクトIDから決まる固定のPVC・Deployment・Serviceだけを作る。
KoyorinaへKubernetesの認証情報を渡さないための境界であり、ここが唯一の特権側になる。
生成コードは共有PVCのプロジェクト別subPathへ展開し、実行Podだけがそれをマウントする。
"""
import asyncio
from contextlib import suppress
import json
import re
from datetime import datetime, timezone
from pathlib import Path
import secrets
import ssl
from typing import Annotated, Literal
from urllib.parse import quote
from uuid import UUID
import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict
from backend.core.request_log import RequestLogMiddleware, configure_logging
from backend.domain.generation import CodeBundle
from backend.domain.tenant_copy import copy_script
from backend.domain.preview import (PreviewPaths, dependency_digest, materialize,
                                    package_source, read_state,
                                    remove_workspace, resource_name, runtime_environment,
                                    startup_expired, write_state)
from backend.domain.preview_env import (CONTROL as CONTROL_CHARS, MAX_ENTRIES as MAX_EXTRA_ENV,
                                        MAX_VALUE_BYTES as MAX_ENV_VALUE, NAME as ENV_NAME,
                                        RESERVED as RESERVED_ENV)
from backend.domain.tenant_storage import (canonical_tenant_id, preview_claim, quantity_bytes,
                                           storage_measurement_pod, tenant_hash)
from backend.domain import gemini_probe
from backend.domain.tenant_ai import CONFIG_FILE, CREDENTIAL_DIR, TOKEN_FILE

# プレビューの作業場所のうち、起動時（setup/preview/entrypoint.sh）に作り直せるもの。
PREVIEW_REBUILT = ("var/venv", "var/cache", "var/python.sha", "var/node.sha")

STARTING = "依存関係の導入と起動を実行中です。完了まで数分かかることがあります。"
STOPPED = "プレビューが停止しました。"
TIMED_OUT = "起動を待ちましたが、アプリが応答しませんでした。"
# 待っているだけではなく、起動を諦めた状態。ここに入ったら「起動中」とは呼ばない。
FAILED_REASONS = {"CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull",
                  "CreateContainerError", "CreateContainerConfigError", "RunContainerError"}
# 調査用のコマンド実行。対話シェルではなく、1回ごとに開いて待って閉じる。
# 上限を置くのは、終わらないビルドで実行環境を占有させないため。
EXEC_TIMEOUT = 120
EXEC_OUTPUT_BYTES = 40_000
EXEC_COMMAND_CHARS = 2000


class ControllerSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="preview_controller_", extra="ignore")
    token: SecretStr = Field(min_length=32)
    # Helmリリース名。許可イメージ名を固定するための運用設定。
    app_name: str = Field(default="koyorina", pattern=r"^[a-z]([a-z0-9-]*[a-z0-9])?$", max_length=44)
    # 既定は "koyorina-preview"。運営がアプリ名を変えた場合は、マニフェストが
    # PREVIEW_CONTROLLER_NAMESPACE で実際のnamespaceを渡す（両者は${APP_NAME}から揃う）。
    namespace: str = Field(default="koyorina-preview", pattern=r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$",
                           max_length=63)
    # Local-only controller tests may use a directory. Shared environments leave this unset
    # and always use tenant PVCs through staging pods.
    root: Path | None = None
    # 取得元は設定で変える。digest固定は保ったまま、別環境へ持っていけるようにする。
    # 既定は開発環境。実際の値はどちらの環境でもマニフェストのConfigMapが渡す
    # （setup/environments/<環境>.env の REGISTRY が正）。
    image_registry: str = Field(default="registry.example.com", max_length=200)
    runtime_image: str = Field(max_length=300)
    # クラスタごとに違う値。ノードは作業領域のディスクが付いているものを指す。
    # RWX（Filestore/NFS）ではどのノードでもマウントできるので、node_selector で
    # ラベル一致に切り替えられる（node_selectorがあればそちらを優先する）。
    node: str = Field(default="k3s-agent-2", max_length=253)
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
    storage_class: str = Field(default="local-path", max_length=253)
    storage_access_mode: Literal["ReadWriteOnce", "ReadWriteMany"] = "ReadWriteOnce"
    storage_size: str = Field(default="20Gi", max_length=20)
    # 旧共有PVCは管理者が開始した移行Podだけが読む。
    legacy_claim: str = Field(default="koyorina-previews",
                              pattern=r"^[a-z0-9.-]+$", max_length=253)
    max_running: int = Field(default=4, ge=1, le=16)
    # 生成アプリ1つぶんの枠。要求はノードの空きを押さえる量、上限は使ってよい量。
    # 依存の導入とビルドで一時的に伸びるので、要求は控えめ・上限は広めに取る。
    # クラスタの大きさで変えたいので、コードではなく設定で持つ。
    cpu_request: str = Field(default="100m", pattern=r"^\d+m?$", max_length=10)
    cpu_limit: str = Field(default="2", pattern=r"^\d+m?$", max_length=10)
    memory_request: str = Field(default="2Gi", pattern=r"^\d+(Mi|Gi)$", max_length=10)
    memory_limit: str = Field(default="3Gi", pattern=r"^\d+(Mi|Gi)$", max_length=10)
    # 依存の取得元。検査済みのレジストリ（Takumi Guardなど）へ向けるために使う。
    # 空なら公開レジストリをそのまま使う。どちらも取得するだけで、秘密は要らない。
    npm_registry: str = Field(default="", max_length=300)
    npm_min_release_age: str = Field(default="", max_length=20)
    pypi_index: str = Field(default="", max_length=300)
    image_pull_secret: str = Field(default="", pattern=r"^[a-z0-9.-]*$", max_length=253)
    agent_toleration: bool = False

    @property
    def preview_label(self) -> str:
        return f"{self.app_name}-preview"

    @model_validator(mode="after")
    def pinned_runtime_image(self):
        """digest固定のまま、取得元だけを設定で変えられるようにする。

        取得元を固定するのは、controllerに任意のイメージを起動させないため。
        """
        expected = re.compile(re.escape(f"{self.image_registry}/{self.app_name}-preview-runtime")
                              + r"@sha256:[0-9a-f]{64}$")
        if not expected.fullmatch(self.runtime_image):
            raise ValueError("プレビュー実行環境のイメージは "
                             "PREVIEW_CONTROLLER_IMAGE_REGISTRY のdigest参照で指定してください。")
        if (self.storage_access_mode == "ReadWriteOnce"
                and not self.node_selector and not self.node):
            raise ValueError("ReadWriteOnce では node か node_selector のどちらかが必要です。")
        return self

    @property
    def effective_node_selector(self) -> dict[str, str]:
        if self.node_selector:
            return dict(self.node_selector)
        if self.node:
            return {"kubernetes.io/hostname": self.node}
        return {}

    def scheduling(self) -> dict:
        selector = self.effective_node_selector
        return {"nodeSelector": selector} if selector else {}


class CommandInput(BaseModel):
    # 改行は通さない。1行1回の実行という形を、受け取る側でも守らせる。
    command: str = Field(min_length=1, max_length=EXEC_COMMAND_CHARS, pattern=r"^[^\r\n]+$")


WIF_AUDIENCE = re.compile(r"https://iam\.googleapis\.com/projects/\d{6,20}/locations/global/"
                          r"workloadIdentityPools/[a-z0-9-]{4,32}/providers/[a-z0-9-]{4,32}")


class WorkloadIdentity(BaseModel):
    """テナントのVertex AI。Kubernetesのトークンを、GCPのWorkload Identity連携で交換させる。"""
    audience: str = Field(max_length=300)
    config: str = Field(max_length=4096)

    @model_validator(mode="after")
    def trusted_shape(self):
        # 設定ファイルは秘密ではないが、読ませるトークンの場所と交換先はここで固定する。
        # 別の場所のファイルや別の交換先を指させない。
        if not WIF_AUDIENCE.fullmatch(self.audience):
            raise ValueError("Workload Identity の audience が不正です。")
        try:
            config = json.loads(self.config)
        except ValueError:
            raise ValueError("Workload Identity の設定が不正です。") from None
        source = config.get("credential_source") if isinstance(config, dict) else None
        if (config.get("type") != "external_account"
                or config.get("token_url") != "https://sts.googleapis.com/v1/token"
                or not isinstance(source, dict)
                or source.get("file") != f"{CREDENTIAL_DIR}/{TOKEN_FILE}"
                or "//" + self.audience.removeprefix("https://") != config.get("audience")):
            raise ValueError("Workload Identity の設定が不正です。")
        return self


def gemini_env(environment: dict, secret_names, secret_name: str) -> list[dict]:
    """平文の環境変数と、Secretを参照する環境変数。秘密は値を定義に書かない。"""
    return ([{"name": key, "value": value} for key, value in sorted(environment.items())]
            + [{"name": key, "valueFrom": {"secretKeyRef": {"name": secret_name, "key": key}}}
               for key in sorted(secret_names)])


def credential_volume(wif: "WorkloadIdentity", config_name: str) -> dict:
    """Kubernetes API のトークンは載せない（automount=false）。載せるのは audience を
    テナントのWIFプロバイダーに限ったトークンだけで、GCPのSTSでしか使えない。
    期限はKubernetesが自動で延ばす。"""
    return {"name": "gcp", "projected": {"defaultMode": 0o444, "sources": [
        {"serviceAccountToken": {"audience": wif.audience, "expirationSeconds": 3600, "path": TOKEN_FILE}},
        {"configMap": {"name": config_name, "items": [{"key": CONFIG_FILE, "path": CONFIG_FILE}]}}]}}


GCP_MOUNT = {"name": "gcp", "mountPath": CREDENTIAL_DIR, "readOnly": True}


def tenant_service_account(tenant_id) -> str:
    """テナントのプレビューが名乗る身元。GCP側はこの名前（subject）を信頼する。"""
    return f"preview-tenant-{tenant_hash(tenant_id, 16)}"


def credential_config_name(project_id) -> str:
    return resource_name(project_id) + "-gcp"


def secret_env_name(project_id) -> str:
    return resource_name(project_id) + "-env"


class LaunchInput(BaseModel):
    job_id: str | None = None
    files: list | None = None
    app_origin: str = Field(max_length=200)
    forward_secret: str = Field(min_length=32, max_length=200)
    google_client_id: str = Field(default="", max_length=200)
    admin_email: str = Field(default="", max_length=320)
    # 画面から指定された環境変数。呼び出し側でも検査しているが、ここは別の
    # 信頼境界なので受け取り直す。名前の規則と予約名はここでも確かめる。
    extra_env: dict[str, str] = Field(default_factory=dict)
    # テナントの秘密（Gemini APIキー）。Secretに入れて secretKeyRef で渡す。
    secret_env: dict[str, str] = Field(default_factory=dict)
    wif: WorkloadIdentity | None = None

    @model_validator(mode="after")
    def safe_environment(self):
        if len(self.extra_env) + len(self.secret_env) > MAX_EXTRA_ENV:
            raise ValueError("環境変数の件数が上限を超えています。")
        if set(self.extra_env) & set(self.secret_env):
            raise ValueError("環境変数の名前が重複しています。")
        for name, value in {**self.extra_env, **self.secret_env}.items():
            if not ENV_NAME.fullmatch(name) or name in RESERVED_ENV:
                raise ValueError("環境変数の名前が不正です。")
            if len(value.encode()) > MAX_ENV_VALUE or CONTROL_CHARS.search(value):
                raise ValueError("環境変数の値が不正です。")
        return self


class ProbeInput(BaseModel):
    """テナントのGeminiの接続テスト。生成アプリと同じ形の環境変数を受け取る。"""
    extra_env: dict[str, str] = Field(default_factory=dict)
    secret_env: dict[str, str] = Field(default_factory=dict)
    wif: WorkloadIdentity | None = None

    @model_validator(mode="after")
    def safe_environment(self):
        for name, value in {**self.extra_env, **self.secret_env}.items():
            if (not ENV_NAME.fullmatch(name) or name in RESERVED_ENV
                    or len(value.encode()) > MAX_ENV_VALUE or CONTROL_CHARS.search(value)):
                raise ValueError("環境変数が不正です。")
        return self


class MigrationInput(BaseModel):
    migration_id: UUID
    project_id: UUID
    source_tenant_id: UUID
    target_tenant_id: UUID
    job_ids: list[UUID] = Field(default_factory=list, max_length=1000)
    legacy_source: bool = False


def manifests(project_id, tenant_id, settings: ControllerSettings = None, environment: dict = None, launched_at: str = None,
              job_id: str | None = None, secret_names=(), wif: "WorkloadIdentity | None" = None):
    """プロジェクトIDから決まる固定の形だけを作る。任意の指定は受け付けない。"""
    if isinstance(tenant_id, ControllerSettings):
        # Compatibility for local manifest inspection; production callers always pass tenant.
        tenant_id, settings, environment, launched_at = (
            "00000000-0000-4000-8000-000000000001", tenant_id, settings, environment)
    name = resource_name(project_id)
    labels = {"app.kubernetes.io/name": name, "app.kubernetes.io/part-of": settings.preview_label,
              "koyorina/tenant": canonical_tenant_id(tenant_id)}
    meta = {"name": name, "namespace": settings.namespace, "labels": labels}
    return {
        "services": {"apiVersion": "v1", "kind": "Service", "metadata": meta,
            "spec": {"selector": labels, "ports": [{"name": "http", "port": 8080, "targetPort": 8080}]}},
        "deployments": {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": meta, "spec": {
            "replicas": 1, "selector": {"matchLabels": labels},
            # 2並行だと同じ /workspace サブパスへ二重にビルドが走る（vite build --watch
            # がその場でdistへ書く作り）。RWXでも同時マウント自体は可能だが、
            # アプリの動作として二重起動を許さない。
            "strategy": {"type": "Recreate"},
            # 起動のたびに注釈を更新してPodを入れ替える。Pod削除の権限を持たせない。
            "template": {"metadata": {"labels": labels,
                                      "annotations": {"koyorina/launched-at": launched_at,
                                                      "koyorina/job-id": job_id or ""}},
                         "spec": {
                "automountServiceAccountToken": False, "enableServiceLinks": False,
                **({"serviceAccountName": tenant_service_account(tenant_id)} if wif else {}),
                **({"imagePullSecrets": [{"name": settings.image_pull_secret}]} if settings.image_pull_secret else {}),
                **({"tolerations": [{"key": "workload", "operator": "Equal",
                                    "value": f"{settings.app_name}-agent", "effect": "NoSchedule"}]}
                   if settings.agent_toleration else {}),
                # RWO（ノード固定型ストレージ）では同じノードへ固定する。RWX(Filestore/NFS)では
                # node_selectorだけで足り、どのノードでも同じ作業領域を見られる。
                **settings.scheduling(),
                "securityContext": {"runAsNonRoot": True, "runAsUser": 10001, "runAsGroup": 10001,
                                    "fsGroup": 10001, "fsGroupChangePolicy": "OnRootMismatch",
                                    "seccompProfile": {"type": "RuntimeDefault"}},
                "containers": [{"name": "app", "image": settings.runtime_image,
                    "securityContext": {"allowPrivilegeEscalation": False,
                                        "capabilities": {"drop": ["ALL"]}},
                    "workingDir": "/workspace",
                    "ports": [{"containerPort": 8080}],
                    "env": gemini_env(environment, secret_names, secret_env_name(project_id)),
                    "resources": {"requests": {"cpu": settings.cpu_request,
                                               "memory": settings.memory_request},
                                  "limits": {"cpu": settings.cpu_limit,
                                             "memory": settings.memory_limit}},
                    "volumeMounts": [
                        {"name": "previews", "mountPath": "/workspace",
                         "subPath": f"{project_id}/workspace"},
                        {"name": "previews", "mountPath": "/var/preview",
                         "subPath": f"{project_id}/var"},
                        # SQLiteだけはPVCから外す。RWX(Filestore/NFS)ではfcntlロックが
                        # rpc.statd/NLM頼みで、`database is locked` や最悪データ破損の
                        # リスクがある。venv/node_modulesキャッシュはPVC側に残してよい
                        # （再スケジュール時の再導入コストを避けるため）。
                        {"name": "dbdata", "mountPath": "/var/preview/db"},
                        {"name": "tmp", "mountPath": "/tmp"}]
                        + ([GCP_MOUNT] if wif else []),
                    # 生成アプリの経路構成に依存しない。前段が必ず応答する専用の口を見る。
                    "startupProbe": {"httpGet": {"path": "/__preview/health", "port": 8080},
                                     "failureThreshold": 120, "periodSeconds": 5},
                    "livenessProbe": {"httpGet": {"path": "/__preview/health", "port": 8080},
                                      "initialDelaySeconds": 30, "periodSeconds": 20, "failureThreshold": 6}}],
                "volumes": [{"name": "previews", "persistentVolumeClaim": {
                                "claimName": preview_claim(tenant_id)}},
                            {"name": "dbdata", "emptyDir": {"sizeLimit": "256Mi"}},
                            {"name": "tmp", "emptyDir": {"sizeLimit": "256Mi"}}]
                           + ([credential_volume(wif, credential_config_name(project_id))] if wif else [])}}}},
    }


class Provisioner:
    def __init__(self, settings):
        self.settings = settings
        self.lock = asyncio.Lock()

    async def kube(self, method, resource, name="", body=None, group="api/v1", query="", text=False):
        token = Path("/var/run/secrets/kubernetes.io/serviceaccount/token").read_text().strip()
        context = ssl.create_default_context(cafile="/var/run/secrets/kubernetes.io/serviceaccount/ca.crt")
        url = f"https://kubernetes.default.svc/{group}/namespaces/{self.settings.namespace}/{resource}"
        if name:
            url += "/" + name
        headers = {"Authorization": "Bearer " + token}
        if method == "PATCH":
            headers["Content-Type"] = "application/apply-patch+yaml"
            query = query or "?fieldManager=koyorina-preview&force=true"
        async with httpx.AsyncClient(verify=context, timeout=20, trust_env=False) as client:
            response = await client.request(method, url + query, headers=headers, json=body)
        if response.status_code == 404:
            return None
        if not response.is_success:
            # APIサーバーの応答はマニフェスト内容を含む。利用者へ返さない。
            raise HTTPException(503, "プレビュー環境を準備できません。管理者に容量・権限の確認を依頼してください。")
        if text:
            return response.text
        return response.json() if response.content else {}

    async def ensure_claim(self, tenant_id):
        name = preview_claim(tenant_id)
        if await self.kube("GET", "persistentvolumeclaims", name) is None:
            await self.kube("POST", "persistentvolumeclaims", body={
                "apiVersion": "v1", "kind": "PersistentVolumeClaim",
                "metadata": {"name": name, "namespace": self.settings.namespace,
                             "labels": {"app": "koyorina-preview-storage",
                                        "koyorina/tenant": canonical_tenant_id(tenant_id)}},
                "spec": {"accessModes": [self.settings.storage_access_mode],
                         "storageClassName": self.settings.storage_class,
                         "resources": {"requests": {"storage": self.settings.storage_size}}}})
        return name

    async def measure_storage(self, tenant_id):
        """Measure exactly one tenant preview claim through a read-only mount."""
        tenant_id = canonical_tenant_id(tenant_id)
        claim = preview_claim(tenant_id)
        pvc = await self.kube("GET", "persistentvolumeclaims", claim)
        base = {"kind": "preview", "claim_name": claim,
                "requested_bytes": quantity_bytes(self.settings.storage_size),
                "measured_at": datetime.now(timezone.utc).isoformat()}
        if pvc is None:
            return {**base, "status": "missing", "used_bytes": 0,
                    "capacity_bytes": 0, "available_bytes": 0}
        requested = (((pvc.get("spec") or {}).get("resources") or {}).get("requests") or {}).get("storage")
        if requested:
            base["requested_bytes"] = quantity_bytes(requested)
        name = "storage-preview-" + tenant_hash(tenant_id, 16)
        with suppress(Exception):
            await self.kube("DELETE", "pods", name)
        pod = storage_measurement_pod(name, self.settings.namespace, claim,
                                      self.settings.runtime_image, self.settings.effective_node_selector,
                                      image_pull_secret=self.settings.image_pull_secret,
                                      toleration=self.settings.agent_toleration,
                                      app_name=self.settings.app_name)
        try:
            await self.kube("POST", "pods", body=pod)
            for _ in range(60):
                current = await self.kube("GET", "pods", name)
                phase = (current or {}).get("status", {}).get("phase")
                if phase == "Succeeded":
                    values = json.loads(await self.kube("GET", "pods", name + "/log", text=True))
                    return {**base, "status": "ok", **values}
                if phase == "Failed":
                    raise HTTPException(503, "プレビュー領域の使用量を計測できませんでした。")
                await asyncio.sleep(1)
            raise HTTPException(503, "プレビュー領域の使用量計測が時間内に完了しませんでした。")
        finally:
            with suppress(Exception):
                await self.kube("DELETE", "pods", name)

    async def migrate(self, payload: MigrationInput):
        await self.ensure_claim(payload.target_tenant_id)
        name = "preview-move-" + payload.migration_id.hex[:20]
        # プレビューの作業場所（依存の生成物は除く）。domain/tenant_copy 参照。
        # 仮想環境とキャッシュは起動時に作り直せる。導入済みの印（*.sha）も一緒に
        # 除かないと、移行先で「導入済み」と判断され、依存の無いまま起動してしまう。
        script = copy_script([(str(payload.project_id), True, PREVIEW_REBUILT)])
        pod = {"apiVersion": "v1", "kind": "Pod", "metadata": {"name": name,
                "namespace": self.settings.namespace, "labels": {"app": "koyorina-preview-migration"}},
            "spec": {"restartPolicy": "Never", "automountServiceAccountToken": False,
                **self.settings.scheduling(),
                "securityContext": {"runAsNonRoot": True, "runAsUser": 10001, "runAsGroup": 10001,
                                    "fsGroup": 10001, "fsGroupChangePolicy": "OnRootMismatch",
                                    "seccompProfile": {"type": "RuntimeDefault"}},
                "containers": [{"name": "copy", "image": self.settings.runtime_image,
                    "command": ["python", "-c", script],
                    "securityContext": {"allowPrivilegeEscalation": False,
                                        "readOnlyRootFilesystem": True, "capabilities": {"drop": ["ALL"]}},
                    "volumeMounts": [{"name": "source", "mountPath": "/source", "readOnly": True},
                                     {"name": "target", "mountPath": "/target"},
                                     {"name": "tmp", "mountPath": "/tmp"}]}],
                            "volumes": [{"name": "source", "persistentVolumeClaim": {
                                "claimName": self.settings.legacy_claim if payload.legacy_source
                                else preview_claim(payload.source_tenant_id)}},
                            {"name": "target", "persistentVolumeClaim": {
                                "claimName": preview_claim(payload.target_tenant_id)}},
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
                raise HTTPException(503, "プレビューデータのコピーに失敗しました。")
            await asyncio.sleep(2)
        raise HTTPException(503, "プレビューデータのコピーが時間内に完了しませんでした。")

    async def cleanup_migration(self, payload: MigrationInput):
        name = "preview-clean-" + payload.migration_id.hex[:20]
        script = ("import shutil\nfrom pathlib import Path\n"
                  f"shutil.rmtree(Path('/source')/{str(payload.project_id)!r},ignore_errors=True)\n")
        claim = (self.settings.legacy_claim if payload.legacy_source
                 else preview_claim(payload.source_tenant_id))
        pod = {"apiVersion": "v1", "kind": "Pod", "metadata": {"name": name,
                "namespace": self.settings.namespace, "labels": {"app": "koyorina-preview-cleanup"}},
            "spec": {"restartPolicy": "Never", "automountServiceAccountToken": False,
                **self.settings.scheduling(),
                "securityContext": {"runAsNonRoot": True, "runAsUser": 10001,
                                    "runAsGroup": 10001, "fsGroup": 10001,
                                    "fsGroupChangePolicy": "OnRootMismatch",
                                    "seccompProfile": {"type": "RuntimeDefault"}},
                "containers": [{"name": "cleanup", "image": self.settings.runtime_image,
                    "command": ["python", "-c", script],
                    "securityContext": {"allowPrivilegeEscalation": False,
                                        "readOnlyRootFilesystem": True,
                                        "capabilities": {"drop": ["ALL"]}},
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
                raise HTTPException(503, "移行元のプレビューデータを削除できませんでした。")
            await asyncio.sleep(2)
        raise HTTPException(503, "移行元のプレビューデータ削除が時間内に完了しませんでした。")

    async def delete_legacy_claim(self):
        await self.kube("DELETE", "persistentvolumeclaims", self.settings.legacy_claim)
        return {"status": "deleted"}

    async def stage(self, tenant_id, project_id, files, state, *, remove=False):
        """Write a validated bundle through a short-lived pod mounting one tenant PVC."""
        import websockets
        claim = await self.ensure_claim(tenant_id)
        name = "preview-stage-" + UUID(str(project_id)).hex[:16] + "-" + secrets.token_hex(3)
        pod = {"apiVersion": "v1", "kind": "Pod", "metadata": {"name": name,
                "namespace": self.settings.namespace, "labels": {"app": "koyorina-preview-stage"}},
            "spec": {"restartPolicy": "Never", "automountServiceAccountToken": False,
                **({"imagePullSecrets": [{"name": self.settings.image_pull_secret}]}
                   if self.settings.image_pull_secret else {}),
                # 実行Podと同じagentノードへ配置できるよう、準備Podにも反映する。
                **({"tolerations": [{"key": "workload", "operator": "Equal",
                                    "value": f"{self.settings.app_name}-agent", "effect": "NoSchedule"}]}
                   if self.settings.agent_toleration else {}),
                **self.settings.scheduling(),
                "securityContext": {"runAsNonRoot": True, "runAsUser": 10001, "runAsGroup": 10001,
                                    "fsGroup": 10001, "fsGroupChangePolicy": "OnRootMismatch",
                                    "seccompProfile": {"type": "RuntimeDefault"}},
                "containers": [{"name": "stage", "image": self.settings.runtime_image,
                    "command": ["sleep", "600"],
                    "securityContext": {"allowPrivilegeEscalation": False,
                                        "readOnlyRootFilesystem": True, "capabilities": {"drop": ["ALL"]}},
                    "resources": {"requests": {"cpu": "10m", "memory": "32Mi"},
                                  "limits": {"cpu": "200m", "memory": "256Mi"}},
                    "volumeMounts": [{"name": "data", "mountPath": "/data"},
                                     {"name": "tmp", "mountPath": "/tmp"}]}],
                "volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": claim}},
                            {"name": "tmp", "emptyDir": {"sizeLimit": "64Mi"}}]}}
        await self.kube("POST", "pods", body=pod)
        try:
            for _ in range(60):
                current = await self.kube("GET", "pods", name)
                if (current or {}).get("status", {}).get("phase") == "Running":
                    break
                await asyncio.sleep(1)
            else:
                raise HTTPException(503, "プレビュー保存領域を準備できませんでした。")
            body = json.dumps({"project": str(UUID(str(project_id))), "files": files or [],
                               "state": state, "remove": remove}, ensure_ascii=False).encode()
            script = """import json,sys,shutil\nfrom pathlib import Path, PurePosixPath\nn=int(sys.argv[1]); p=json.loads(sys.stdin.buffer.read(n)); root=Path('/data')/p['project']\nif p['remove']:\n shutil.rmtree(root,ignore_errors=True); raise SystemExit(0)\nworkspace=root/'workspace'; var=root/'var'; workspace.mkdir(parents=True,exist_ok=True); var.mkdir(parents=True,exist_ok=True)\nkeep=set()\nfor item in p['files']:\n rel=PurePosixPath(item['path'])\n if rel.is_absolute() or '..' in rel.parts: raise ValueError('unsafe path')\n target=workspace.joinpath(*rel.parts); target.parent.mkdir(parents=True,exist_ok=True); target.write_text(item['content'],encoding='utf-8'); keep.add(str(rel))\nfor path in sorted(workspace.rglob('*'),reverse=True):\n rel=str(path.relative_to(workspace))\n if path.is_file() and rel not in keep and not rel.startswith(('.venv/','frontend/node_modules/')): path.unlink()\n(var/'state.json').write_text(json.dumps(p['state']),encoding='utf-8')\n"""
            token = Path("/var/run/secrets/kubernetes.io/serviceaccount/token").read_text().strip()
            context = ssl.create_default_context(cafile="/var/run/secrets/kubernetes.io/serviceaccount/ca.crt")
            commands = ["python", "-c", script, str(len(body))]
            query = "&".join(["stdout=true", "stderr=true", "stdin=true", "tty=false",
                              "container=stage", *["command=" + quote(value, safe="") for value in commands]])
            url = (f"wss://kubernetes.default.svc/api/v1/namespaces/{self.settings.namespace}"
                   f"/pods/{name}/exec?{query}")
            status = {}
            async with websockets.connect(url, ssl=context,
                    additional_headers={"Authorization": "Bearer " + token},
                    subprotocols=["v4.channel.k8s.io"], open_timeout=20,
                    max_size=max(len(body) * 2, 1_000_000)) as socket:
                for offset in range(0, len(body), 64 * 1024):
                    await socket.send(b"\x00" + body[offset:offset + 64 * 1024])
                async with asyncio.timeout(120):
                    async for frame in socket:
                        if isinstance(frame, (bytes, bytearray)) and frame and frame[0] == 3:
                            with suppress(ValueError):
                                status = json.loads(bytes(frame[1:]).decode("utf-8", "replace"))
            if status.get("status") != "Success":
                raise HTTPException(503, "プレビュー保存領域へ書き込めませんでした。")
        finally:
            with suppress(Exception):
                await self.kube("DELETE", "pods", name)

    async def running(self):
        listing = await self.kube("GET", "deployments", group="apis/apps/v1",
                                  query=f"?labelSelector=app.kubernetes.io/part-of={self.settings.preview_label}")
        return [item["metadata"]["name"] for item in (listing or {}).get("items", [])]

    def paths(self, project_id):
        if self.settings.root is None:
            raise RuntimeError("preview paths are stored in a tenant PVC")
        return PreviewPaths(self.settings.root, project_id).prepare()

    async def project_deployment(self, tenant_id, project_id):
        deployment = await self.kube("GET", "deployments", resource_name(project_id),
                                     group="apis/apps/v1")
        if deployment is not None:
            labels = (deployment.get("metadata") or {}).get("labels") or {}
            recorded = labels.get("koyorina/tenant")
            if recorded and recorded != canonical_tenant_id(tenant_id):
                raise HTTPException(404, "対象テナントのプレビューが見つかりません。")
        return deployment

    async def start(self, tenant_id, project_id=None, payload: LaunchInput = None):
        if payload is None:
            tenant_id, project_id, payload = (
                "00000000-0000-4000-8000-000000000001", tenant_id, project_id)
        async with self.lock:
            await self.project_deployment(tenant_id, project_id)
            paths = self.paths(project_id) if self.settings.root is not None else None
            state = read_state(paths) if paths else {}
            if payload.files is not None:
                bundle = CodeBundle.model_validate({"files": payload.files})
                if paths:
                    materialize(paths.workspace, bundle)
                state["job_id"] = payload.job_id
            elif not state.get("job_id"):
                raise HTTPException(409, "先に生成済みのコードからプレビューを開始してください。")
            name = resource_name(project_id)
            running = await self.running()
            if name not in running and len(running) >= self.settings.max_running:
                raise HTTPException(409, "同時に実行できるプレビューの上限に達しています。使っていないものを停止してください。")
            state.update({"updated_at": datetime.now(timezone.utc).isoformat(),
                          "session_secret": secrets.token_urlsafe(48)})
            if paths:
                state["dependency_digest"] = dependency_digest(paths.workspace)
                write_state(paths, state)
            else:
                await self.stage(tenant_id, project_id, bundle.model_dump()["files"], state)
            environment = runtime_environment(project_id, payload.app_origin, state["session_secret"],
                                              payload.forward_secret, payload.google_client_id,
                                              payload.admin_email, extra=payload.extra_env,
                                              packages=package_source(
                                                  self.settings.npm_registry,
                                                  self.settings.npm_min_release_age,
                                                  self.settings.pypi_index))
            shapes = manifests(project_id, tenant_id, self.settings, environment, state["updated_at"],
                               state.get("job_id"), secret_names=list(payload.secret_env), wif=payload.wif)
            await self.tenant_credentials(tenant_id, project_id, payload)
            await self.kube("PATCH", "services", name, shapes["services"])
            await self.kube("PATCH", "deployments", name, shapes["deployments"], group="apis/apps/v1")
            return await self.status(tenant_id, project_id)

    async def tenant_credentials(self, tenant_id, project_id, payload):
        """テナントのGeminiに要るものを、Deploymentより先に用意する。使わないものは消す。

        秘密はSecretに入れ、Deploymentの定義に平文で残さない。WIFの設定ファイルは秘密では
        ないがプロジェクトごとのConfigMapに置き、テナントの身元はServiceAccountで表す。
        """
        labels = {"app.kubernetes.io/part-of": self.settings.preview_label,
                  "koyorina/tenant": canonical_tenant_id(tenant_id)}
        ns = self.settings.namespace
        if payload.wif:
            account = tenant_service_account(tenant_id)
            await self.kube("PATCH", "serviceaccounts", account, {
                "apiVersion": "v1", "kind": "ServiceAccount",
                "metadata": {"name": account, "namespace": ns, "labels": labels},
                "automountServiceAccountToken": False})
            await self.kube("PATCH", "configmaps", credential_config_name(project_id), {
                "apiVersion": "v1", "kind": "ConfigMap",
                "metadata": {"name": credential_config_name(project_id), "namespace": ns, "labels": labels},
                "data": {CONFIG_FILE: payload.wif.config}})
        else:
            await self.kube("DELETE", "configmaps", credential_config_name(project_id))
        if payload.secret_env:
            await self.kube("PATCH", "secrets", secret_env_name(project_id), {
                "apiVersion": "v1", "kind": "Secret", "type": "Opaque",
                "metadata": {"name": secret_env_name(project_id), "namespace": ns, "labels": labels},
                "stringData": dict(payload.secret_env)})
        else:
            await self.kube("DELETE", "secrets", secret_env_name(project_id))

    async def probe_gemini(self, tenant_id, payload: "ProbeInput"):
        """テナントの身元で使い捨てのPodを動かし、Geminiへ1回だけ問い合わせる。

        生成アプリと同じnamespace・同じ通信制限・同じ身元で確かめる。Koyorina本体から
        呼ぶと、本体では通るのにアプリでは通らない、という食い違いを見逃す。
        """
        name = f"gemini-probe-{tenant_hash(tenant_id, 8)}-{secrets.token_hex(3)}"
        ns = self.settings.namespace
        labels = {"app.kubernetes.io/part-of": self.settings.preview_label,
                  "koyorina/tenant": canonical_tenant_id(tenant_id), "koyorina/probe": "gemini"}
        if payload.wif:
            account = tenant_service_account(tenant_id)
            await self.kube("PATCH", "serviceaccounts", account, {
                "apiVersion": "v1", "kind": "ServiceAccount",
                "metadata": {"name": account, "namespace": ns, "labels": labels},
                "automountServiceAccountToken": False})
            await self.kube("POST", "configmaps", body={
                "apiVersion": "v1", "kind": "ConfigMap",
                "metadata": {"name": name, "namespace": ns, "labels": labels},
                "data": {CONFIG_FILE: payload.wif.config}})
        if payload.secret_env:
            await self.kube("POST", "secrets", body={
                "apiVersion": "v1", "kind": "Secret", "type": "Opaque",
                "metadata": {"name": name, "namespace": ns, "labels": labels},
                "stringData": dict(payload.secret_env)})
        pod = {"apiVersion": "v1", "kind": "Pod", "metadata": {"name": name, "namespace": ns, "labels": labels},
            "spec": {"restartPolicy": "Never", "activeDeadlineSeconds": 90,
                "automountServiceAccountToken": False, "enableServiceLinks": False,
                **({"serviceAccountName": tenant_service_account(tenant_id)} if payload.wif else {}),
                **({"imagePullSecrets": [{"name": self.settings.image_pull_secret}]}
                   if self.settings.image_pull_secret else {}),
                **({"tolerations": [{"key": "workload", "operator": "Equal",
                                    "value": f"{self.settings.app_name}-agent", "effect": "NoSchedule"}]}
                   if self.settings.agent_toleration else {}),
                **self.settings.scheduling(),
                "securityContext": {"runAsNonRoot": True, "runAsUser": 10001, "runAsGroup": 10001,
                                    "seccompProfile": {"type": "RuntimeDefault"}},
                "containers": [{"name": "probe", "image": self.settings.runtime_image,
                    "command": ["python", "-c", gemini_probe.source()],
                    "env": gemini_env(payload.extra_env, list(payload.secret_env), name),
                    "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                                        "capabilities": {"drop": ["ALL"]}},
                    "resources": {"requests": {"cpu": "10m", "memory": "32Mi"},
                                  "limits": {"cpu": "200m", "memory": "128Mi"}},
                    "volumeMounts": [GCP_MOUNT] if payload.wif else []}],
                "volumes": [credential_volume(payload.wif, name)] if payload.wif else []}}
        try:
            await self.kube("POST", "pods", body=pod)
            phase = ""
            for _ in range(100):
                current = await self.kube("GET", "pods", name) or {}
                phase = current.get("status", {}).get("phase", "")
                if phase in {"Succeeded", "Failed"}:
                    break
                await asyncio.sleep(1)
            else:
                return {"ok": False, "step": "pod", "message": "テスト用のPodが時間内に終わりませんでした。"}
            output = await self.kube("GET", "pods", name + "/log", text=True) or ""
            for line in reversed(output.splitlines()):
                if line.startswith("GEMINI_PROBE "):
                    result = json.loads(line.removeprefix("GEMINI_PROBE "))
                    return {key: result.get(key) for key in
                            ("ok", "step", "status", "message", "model", "text", "elapsed_ms")}
            return {"ok": False, "step": "pod", "message": f"テスト用のPodが結果を返しませんでした（{phase}）。"}
        finally:
            await self.kube("DELETE", "pods", name)
            await self.kube("DELETE", "secrets", name)
            await self.kube("DELETE", "configmaps", name)

    async def workload_identity(self, tenant_id):
        """GCP側で信頼を登録するための値。発行元と公開鍵はクラスタから読む（秘密ではない）。"""
        discovery = await self.cluster_get("/.well-known/openid-configuration")
        jwks = await self.cluster_get("/openid/v1/jwks")
        account = tenant_service_account(tenant_id)
        return {"namespace": self.settings.namespace, "service_account": account,
                "subject": f"system:serviceaccount:{self.settings.namespace}:{account}",
                "issuer": (discovery or {}).get("issuer", ""), "jwks": jwks or {}}

    async def cluster_get(self, path):
        """namespace に属さない読み取り（OIDCの発見用）。kube() は namespace 前提なので分ける。"""
        token = Path("/var/run/secrets/kubernetes.io/serviceaccount/token").read_text().strip()
        context = ssl.create_default_context(cafile="/var/run/secrets/kubernetes.io/serviceaccount/ca.crt")
        async with httpx.AsyncClient(verify=context, timeout=20, trust_env=False) as client:
            response = await client.get("https://kubernetes.default.svc" + path,
                                        headers={"Authorization": "Bearer " + token})
        if not response.is_success:
            raise HTTPException(503, "クラスタのOIDC情報を読めませんでした。")
        return response.json()

    async def stop(self, tenant_id, project_id=None):
        if project_id is None:
            tenant_id, project_id = "00000000-0000-4000-8000-000000000001", tenant_id
        await self.project_deployment(tenant_id, project_id)
        name = resource_name(project_id)
        await self.kube("DELETE", "deployments", name, group="apis/apps/v1")
        await self.kube("DELETE", "services", name)
        # 止めたプレビューの秘密と設定を残さない。テナントのServiceAccountは共有なので残す。
        await self.kube("DELETE", "secrets", secret_env_name(project_id))
        await self.kube("DELETE", "configmaps", credential_config_name(project_id))
        return await self.status(tenant_id, project_id)

    async def discard(self, tenant_id, project_id=None):
        if project_id is None:
            tenant_id, project_id = "00000000-0000-4000-8000-000000000001", tenant_id
        await self.stop(tenant_id, project_id)
        if self.settings.root is not None:
            remove_workspace(PreviewPaths(self.settings.root, project_id))
        else:
            await self.stage(tenant_id, project_id, [], {}, remove=True)
        return await self.status(tenant_id, project_id)

    async def status(self, tenant_id, project_id=None):
        if project_id is None:
            tenant_id, project_id = "00000000-0000-4000-8000-000000000001", tenant_id
        local_state = (read_state(PreviewPaths(self.settings.root, project_id))
                       if self.settings.root is not None else {})
        result = {"state": "stopped", "port": None, "job_id": local_state.get("job_id"),
                  "updated_at": local_state.get("updated_at"), "message": None}
        deployment = await self.project_deployment(tenant_id, project_id)
        if deployment is None:
            return result
        annotations = (((deployment.get("spec") or {}).get("template") or {}).get("metadata") or {}).get("annotations") or {}
        if not local_state:
            result.update({"job_id": annotations.get("koyorina/job-id") or None,
                           "updated_at": annotations.get("koyorina/launched-at")})
        status = deployment.get("status", {})
        if status.get("readyReplicas"):
            return {**result, "state": "running"}
        # 「まだ揃っていない」ことは失敗の証拠ではない。作った直後は必ずこの形を通る。
        # 以前はここを失敗と呼んでいて、開始した瞬間に異常終了と表示していた。
        # 落ちた証拠があるときと、待つ時間を使い切ったときだけ失敗と呼ぶ。
        if await self.crashing(project_id):
            return {**result, "state": "failed", "message": STOPPED}
        if startup_expired({"updated_at": result["updated_at"]}):
            return {**result, "state": "failed", "message": TIMED_OUT}
        return {**result, "state": "starting", "message": STARTING}

    async def pods(self, project_id):
        name = resource_name(project_id)
        listing = await self.kube("GET", "pods", query=f"?labelSelector=app.kubernetes.io/name={name}")
        return (listing or {}).get("items", [])

    async def crashing(self, project_id):
        """起動を諦めた、または起動して終了した状態か。待っているだけの間はFalse。"""
        for pod in await self.pods(project_id):
            for container in pod.get("status", {}).get("containerStatuses", []):
                state = container.get("state") or {}
                if (state.get("waiting") or {}).get("reason") in FAILED_REASONS:
                    return True
                if (state.get("terminated") or {}).get("exitCode"):
                    return True
        return False

    async def logs(self, tenant_id, project_id=None):
        if project_id is None:
            tenant_id, project_id = "00000000-0000-4000-8000-000000000001", tenant_id
        await self.project_deployment(tenant_id, project_id)
        items = await self.pods(project_id)
        if not items:
            return ""
        pod = items[0]
        query = "?tailLines=200&timestamps=false"
        # 落ちて起動し直した直後は、いまのコンテナに何も出ていない。理由は前回の出力にある。
        if any((container.get("restartCount") or 0) > 0
               for container in pod.get("status", {}).get("containerStatuses", [])):
            query += "&previous=true"
        raw = await self.kube("GET", "pods", pod["metadata"]["name"] + "/log", query=query, text=True)
        if not (raw or "").strip() and "previous" in query:
            raw = await self.kube("GET", "pods", pod["metadata"]["name"] + "/log",
                                  query="?tailLines=200&timestamps=false", text=True)
        # 生成アプリの出力は信用しない文字列として扱う。
        return (raw if isinstance(raw, str) else "")[-20000:]

    async def execute(self, tenant_id, project_id=None, command: str | None = None):
        """動いているプレビューの中でコマンドを1回だけ実行する。

        触れるのはそのプレビュー自身の作業場所とSQLiteだけ。生成アプリのコンテナには
        Codexの認証もVertexの鍵もサービスアカウントトークンも載っておらず、非rootで
        capabilityも全部落としてある。依頼できるのは、そのコードとデータをすでに
        見られる人（開発の席を持つ人）に限っている。

        Kubernetesのexecはストリームなのでhttpxでは話せない。WebSocketの
        v4.channel.k8s.io を使う（各フレームの先頭1バイトがチャネル番号。
        1=stdout, 2=stderr, 3=終了状態）。対話はしないので stdin は開かない。
        """
        if command is None:
            tenant_id, project_id, command = (
                "00000000-0000-4000-8000-000000000001", tenant_id, project_id)
        await self.project_deployment(tenant_id, project_id)
        import json as jsonlib
        import websockets

        ready = [pod for pod in await self.pods(project_id)
                 if any(condition.get("type") == "Ready" and condition.get("status") == "True"
                        for condition in pod.get("status", {}).get("conditions", []))]
        if not ready:
            raise HTTPException(409, "プレビューが動いていません。先に起動してください。")
        text = (command or "").strip()
        if not text or len(text) > EXEC_COMMAND_CHARS:
            raise HTTPException(422, "コマンドが空、または長すぎます。")

        token = Path("/var/run/secrets/kubernetes.io/serviceaccount/token").read_text().strip()
        context = ssl.create_default_context(
            cafile="/var/run/secrets/kubernetes.io/serviceaccount/ca.crt")
        query = "&".join(["stdout=true", "stderr=true", "stdin=false", "tty=false",
                          "container=app", "command=/bin/sh", "command=-c",
                          "command=" + quote(text, safe="")])
        url = (f"wss://kubernetes.default.svc/api/v1/namespaces/{self.settings.namespace}"
               f"/pods/{ready[0]['metadata']['name']}/exec?{query}")
        out, err, status = [], [], {}
        try:
            async with websockets.connect(
                    url, ssl=context, additional_headers={"Authorization": "Bearer " + token},
                    subprotocols=["v4.channel.k8s.io"], open_timeout=20,
                    max_size=EXEC_OUTPUT_BYTES * 4) as socket:
                async with asyncio.timeout(EXEC_TIMEOUT):
                    async for frame in socket:
                        if not isinstance(frame, (bytes, bytearray)) or not frame:
                            continue
                        channel, payload = frame[0], bytes(frame[1:])
                        if channel == 1:
                            out.append(payload)
                        elif channel == 2:
                            err.append(payload)
                        elif channel == 3:
                            with suppress(ValueError):
                                status = jsonlib.loads(payload.decode("utf-8", "replace"))
        except TimeoutError:
            err.append(f"\n[打ち切り] {EXEC_TIMEOUT}秒で終わりませんでした。".encode())
            status = {"status": "Timeout"}
        except (OSError, websockets.WebSocketException):
            # APIサーバーの応答はクラスタの事情を含む。利用者へは渡さない。
            raise HTTPException(503, "プレビューの中でコマンドを実行できませんでした。"
                                     "起動しているか確認してください。") from None

        def cut(chunks):
            # 生成アプリの出力は信用しない文字列として扱う。長さだけ切る。
            return b"".join(chunks)[-EXEC_OUTPUT_BYTES:].decode("utf-8", "replace")

        # 終了コードは channel 3 の JSON にしか入らない。取れなければ「不明」を返す。
        # 0 を仮に入れると、失敗したコマンドが成功したように見える。
        code = 0 if status.get("status") == "Success" else None
        for cause in (status.get("details") or {}).get("causes") or []:
            if cause.get("reason") == "ExitCode" and str(cause.get("message", "")).isdigit():
                code = int(cause["message"])
        return {"command": text, "stdout": cut(out), "stderr": cut(err), "exit_code": code}


def create_controller(settings=None):
    configure_logging()
    settings = settings or ControllerSettings()
    provisioner = Provisioner(settings)
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.provisioner = provisioner

    @app.middleware("http")
    async def authenticate(request: Request, call_next):
        if request.url.path != "/healthz" and not secrets.compare_digest(
                request.headers.get("authorization", ""), "Bearer " + settings.token.get_secret_value()):
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(Exception)
    async def safe_error(request, exc):
        return JSONResponse({"error": "プレビュー実行環境へ接続できません。"}, status_code=503)

    @app.get("/healthz")
    async def health():
        return {"status": "ok"}

    @app.post("/migrations")
    async def migrate(payload: MigrationInput):
        return await provisioner.migrate(payload)

    @app.post("/migrations/cleanup")
    async def cleanup_migration(payload: MigrationInput):
        return await provisioner.cleanup_migration(payload)

    @app.delete("/migrations/legacy-claim")
    async def delete_legacy_claim():
        return await provisioner.delete_legacy_claim()

    @app.get("/tenants/{tenant_id}/storage")
    async def storage(tenant_id: UUID):
        async with provisioner.lock:
            return await provisioner.measure_storage(tenant_id)

    @app.post("/tenants/{tenant_id}/ai-probe")
    async def ai_probe(tenant_id: UUID, payload: ProbeInput):
        return await provisioner.probe_gemini(tenant_id, payload)

    @app.get("/tenants/{tenant_id}/oidc")
    async def oidc(tenant_id: UUID):
        return await provisioner.workload_identity(tenant_id)

    @app.put("/tenants/{tenant_id}/projects/{project_id}")
    async def start(tenant_id: UUID, project_id: UUID, payload: LaunchInput):
        return await provisioner.start(tenant_id, project_id, payload)

    @app.get("/tenants/{tenant_id}/projects/{project_id}")
    async def status(tenant_id: UUID, project_id: UUID):
        return await provisioner.status(tenant_id, project_id)

    @app.delete("/tenants/{tenant_id}/projects/{project_id}")
    async def stop(tenant_id: UUID, project_id: UUID):
        return await provisioner.stop(tenant_id, project_id)

    @app.delete("/tenants/{tenant_id}/projects/{project_id}/workspace")
    async def discard(tenant_id: UUID, project_id: UUID):
        return await provisioner.discard(tenant_id, project_id)

    @app.post("/tenants/{tenant_id}/projects/{project_id}/exec")
    async def execute(tenant_id: UUID, project_id: UUID, payload: CommandInput):
        return await provisioner.execute(tenant_id, project_id, payload.command)

    @app.get("/tenants/{tenant_id}/projects/{project_id}/logs")
    async def logs(tenant_id: UUID, project_id: UUID):
        return {"logs": await provisioner.logs(tenant_id, project_id)}

    # 一番外側に置く。認証で断った呼び出しも1行残す。
    app.add_middleware(RequestLogMiddleware)
    return app
