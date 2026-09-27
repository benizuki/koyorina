"""プレビュー実行基盤の切り替え。Koyorina本体は固定の操作しか呼ばない。

- docker: ローカル検証。Dockerデーモンを直接操作するため APP_ENV=local に限定する。
- controller: 共有環境。専用namespaceの最小権限コントローラへHTTPで委譲する。
  KoyorinaはKubernetes APIの認証情報を持たない。
"""
from datetime import datetime, timezone
import secrets
from uuid import UUID
import httpx
from fastapi import HTTPException
from backend.core import preview_runtime as runtime
from backend.domain.preview import (PreviewPaths, allocate_port, dependency_digest,
                                    materialize, package_source,
                                    read_state, remove_workspace, runtime_environment,
                                    service_target, startup_expired, write_state)

STARTING = "依存関係の導入と起動を実行中です。完了まで数分かかることがあります。"
STOPPED = "プレビューが停止しました。"
TIMED_OUT = "起動を待ちましたが、アプリが応答しませんでした。"
NO_SOURCE = "先に生成済みのコードからプレビューを開始してください。"
NOT_RUNNING = "プレビューが動いていません。先に起動してください。"


def launch_state(paths, job_id):
    """起動ごとの状態と秘密を用意する。秘密は作り直さず引き継ぐ。"""
    state = read_state(paths)
    if job_id:
        state["job_id"] = job_id
    elif not state.get("job_id"):
        raise HTTPException(409, NO_SOURCE)
    state.update({"updated_at": datetime.now(timezone.utc).isoformat(),
                  "dependency_digest": dependency_digest(paths.workspace),
                  "session_secret": state.get("session_secret") or secrets.token_urlsafe(48)})
    return state


class DockerBackend:
    """ローカル検証専用。ワークスペースはKoyorinaのディスク、実行はDocker。"""

    def __init__(self, settings):
        self.settings = settings

    def paths(self, project_id):
        self.settings.preview_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        return PreviewPaths(self.settings.preview_root, project_id).prepare()

    async def start(self, project_id, bundle, job_id, context, tenant_id=None):
        paths = self.paths(project_id)
        if bundle is not None:
            materialize(paths.workspace, bundle)
        state = launch_state(paths, job_id)
        state["port"] = allocate_port(self.settings.preview_root, project_id,
                                      self.settings.preview_port_base, self.settings.preview_port_count)
        write_state(paths, state)
        # ローカル検証では秘密もそのまま環境変数で渡す（Secretの仕組みが無い）。
        # Workload Identity連携はKubernetesのトークンが要るので、ここでは使えない。
        extra = {**(context.get("extra_env") or {}), **(context.get("secret_env") or {})}
        environment = runtime_environment(project_id, context["app_origin"], state["session_secret"],
                                          context["forward_secret"], context["google_client_id"],
                                          context["admin_email"], extra=extra,
                                          packages=package_source(
                                              self.settings.preview_npm_registry,
                                              self.settings.preview_npm_min_release_age,
                                              self.settings.preview_pypi_index))
        network = self.settings.preview_docker_network
        await runtime.run(project_id, paths, state["port"], self.settings.preview_image, environment,
                          **({"network": network} if network else {}))
        return await self.status(project_id)

    async def workload_identity(self, tenant_id):
        raise HTTPException(409, "ローカル検証（docker）では Workload Identity 連携を使えません。")

    async def probe_gemini(self, tenant_id, plain, secret, wif):
        """ローカル検証では本体の中で同じ問い合わせを行う。WIFはKubernetesのトークンが要る。"""
        if wif:
            return {"ok": False, "step": "credentials",
                    "message": "ローカル検証（docker）では Workload Identity 連携を試せません。"}
        from starlette.concurrency import run_in_threadpool
        from backend.domain import gemini_probe
        return await run_in_threadpool(gemini_probe.run, {**plain, **secret})

    async def stop(self, project_id, tenant_id=None):
        await runtime.remove(project_id)
        return await self.status(project_id)

    async def discard(self, project_id, tenant_id=None):
        await runtime.remove(project_id)
        remove_workspace(PreviewPaths(self.settings.preview_root, project_id))
        return await self.status(project_id)

    async def status(self, project_id, tenant_id=None):
        state = read_state(PreviewPaths(self.settings.preview_root, project_id))
        result = {"state": "stopped", "port": state.get("port"), "job_id": state.get("job_id"),
                  "updated_at": state.get("updated_at"), "message": None}
        if not state:
            return result
        container = await runtime.container_state(project_id)
        if container == "none":
            return result
        if container == "exited":
            return {**result, "state": "failed", "message": STOPPED}
        port, host = self.address(project_id, state)
        if container == "running" and await (runtime.responding(port, host) if host != "127.0.0.1"
                                               else runtime.responding(port)):
            return {**result, "state": "running"}
        # まだ応答が無いだけでは失敗と呼ばない。ただし待つ時間には上限を置く。
        if startup_expired(state):
            return {**result, "state": "failed", "message": TIMED_OUT}
        return {**result, "state": "starting", "message": STARTING}

    async def logs(self, project_id, tenant_id=None):
        return await runtime.logs(project_id)

    async def execute(self, project_id, command, tenant_id=None):
        return await runtime.execute(project_id, command)

    def address(self, project_id, state) -> tuple[int, str]:
        """(ポート, ホスト)。Compose版はDocker網の中でコンテナ名に届く。"""
        if self.settings.preview_docker_network:
            return 8080, runtime.container_name(project_id)
        return int(state["port"]), "127.0.0.1"

    def target(self, project_id):
        state = read_state(PreviewPaths(self.settings.preview_root, project_id))
        if not state.get("port"):
            return None
        port, host = self.address(project_id, state)
        return f"http://{host}:{port}"

    async def measure_storage(self, tenant_id):
        return {"kind": "preview", "claim_name": "local-preview", "status": "unavailable",
                "requested_bytes": 0, "used_bytes": 0, "capacity_bytes": 0,
                "available_bytes": 0, "measured_at": datetime.now(timezone.utc).isoformat()}


class ControllerBackend:
    """専用コントローラへ委譲する。操作は固定、対象はプロジェクトIDだけ。"""

    def __init__(self, settings):
        self.settings = settings

    async def call(self, method, project_id, tenant_id, path="", body=None, timeout=55, conflict=NO_SOURCE):
        identity = str(UUID(str(project_id)))
        tenant = str(UUID(str(tenant_id)))
        url = f"{self.settings.preview_controller_url}/tenants/{tenant}/projects/{identity}{path}"
        token = self.settings.preview_controller_token.get_secret_value()
        try:
            async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
                response = await client.request(method, url, headers={"Authorization": "Bearer " + token},
                                                json=body)
            if response.status_code == 409:
                raise HTTPException(409, conflict)
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError):
            raise HTTPException(503, "プレビュー実行環境を準備中、または接続できません。少し待って再度お試しください。") from None

    async def start(self, project_id, bundle, job_id, context, tenant_id=None):
        return await self.call("PUT", project_id, tenant_id, body={
            "job_id": job_id, "files": bundle.model_dump()["files"] if bundle else None,
            "app_origin": context["app_origin"], "forward_secret": context["forward_secret"],
            "google_client_id": context["google_client_id"], "admin_email": context["admin_email"],
            "extra_env": context.get("extra_env") or {},
            # テナントのGemini。秘密はSecret、Vertex AI はWorkload Identity連携で渡させる。
            "secret_env": context.get("secret_env") or {}, "wif": context.get("wif")})

    async def stop(self, project_id, tenant_id=None):
        return await self.call("DELETE", project_id, tenant_id)

    async def discard(self, project_id, tenant_id=None):
        return await self.call("DELETE", project_id, tenant_id, "/workspace")

    async def status(self, project_id, tenant_id=None):
        return await self.call("GET", project_id, tenant_id)

    async def logs(self, project_id, tenant_id=None):
        return (await self.call("GET", project_id, tenant_id, "/logs")).get("logs", "")

    async def execute(self, project_id, command, tenant_id=None):
        # 打ち切りはコントローラ側が持つ。こちらはそれより長く待つ。
        # 先に切ると、実行は続いているのに結果だけ捨てることになる。
        return await self.call("POST", project_id, tenant_id, "/exec", {"command": command}, timeout=150,
                               conflict=NOT_RUNNING)

    def target(self, project_id):
        return service_target(project_id, namespace=f"{self.settings.app_name}-preview")

    async def probe_gemini(self, tenant_id, plain, secret, wif):
        """テナントの身元で動く使い捨てのPodから、Geminiへ1回問い合わせてもらう。"""
        tenant = str(UUID(str(tenant_id)))
        token = self.settings.preview_controller_token.get_secret_value()
        try:
            async with httpx.AsyncClient(timeout=150, trust_env=False) as client:
                response = await client.post(f"{self.settings.preview_controller_url}/tenants/{tenant}/ai-probe",
                                             headers={"Authorization": "Bearer " + token},
                                             json={"extra_env": plain, "secret_env": secret, "wif": wif})
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError):
            raise HTTPException(503, "プレビュー実行環境へ接続できず、テストできませんでした。") from None

    async def workload_identity(self, tenant_id):
        """テナントのプレビューが名乗る身元と、クラスタのOIDC発行元・公開鍵。"""
        tenant = str(UUID(str(tenant_id)))
        token = self.settings.preview_controller_token.get_secret_value()
        try:
            async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
                response = await client.get(f"{self.settings.preview_controller_url}/tenants/{tenant}/oidc",
                                            headers={"Authorization": "Bearer " + token})
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError):
            raise HTTPException(503, "クラスタのOIDC情報を取得できませんでした。少し待って再度お試しください。") from None

    async def migrate(self, body):
        token = self.settings.preview_controller_token.get_secret_value()
        try:
            async with httpx.AsyncClient(timeout=420, trust_env=False) as client:
                response = await client.post(self.settings.preview_controller_url + "/migrations",
                    headers={"Authorization": "Bearer " + token}, json=body)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPError:
            raise HTTPException(503, "プレビューデータのテナント移行に失敗しました。") from None

    async def cleanup_migration(self, body):
        token = self.settings.preview_controller_token.get_secret_value()
        try:
            async with httpx.AsyncClient(timeout=420, trust_env=False) as client:
                response = await client.post(self.settings.preview_controller_url + "/migrations/cleanup",
                    headers={"Authorization": "Bearer " + token}, json=body)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPError:
            raise HTTPException(503, "移行元のプレビューデータを削除できませんでした。") from None

    async def delete_legacy_claim(self):
        token = self.settings.preview_controller_token.get_secret_value()
        try:
            async with httpx.AsyncClient(timeout=55, trust_env=False) as client:
                response = await client.delete(self.settings.preview_controller_url + "/migrations/legacy-claim",
                    headers={"Authorization": "Bearer " + token})
            response.raise_for_status()
            return response.json()
        except httpx.HTTPError:
            raise HTTPException(503, "旧プレビュー保存領域を削除できませんでした。") from None

    async def measure_storage(self, tenant_id):
        tenant = str(UUID(str(tenant_id)))
        token = self.settings.preview_controller_token.get_secret_value()
        try:
            async with httpx.AsyncClient(timeout=90, trust_env=False) as client:
                response = await client.get(
                    f"{self.settings.preview_controller_url}/tenants/{tenant}/storage",
                    headers={"Authorization": "Bearer " + token})
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError):
            raise HTTPException(503, "プレビュー領域の使用量を取得できませんでした。") from None


def backend(settings):
    return ControllerBackend(settings) if settings.preview_backend == "controller" else DockerBackend(settings)
