"""Fixed operations against the trusted internal Codex controller."""
import re
from uuid import UUID
import httpx
from fastapi import HTTPException


WAITING = "AppGenの実行環境を準備しています。少し待って再度お試しください。"


def notice(response) -> str:
    """実行環境からの案内をそのまま渡す。内部サービスの固定文で、利用者の入力ではない。"""
    try:
        payload = response.json()
        message = payload.get("error") or payload.get("detail")
    except ValueError:
        return WAITING
    return message if isinstance(message, str) and 0 < len(message) <= 600 else WAITING


async def controller(settings, user_id, method, path, body=None, *, tenant_id=None):
    if not settings.codex_controller_url:
        raise HTTPException(503, "AppGenの実行環境が未設定です。管理者に設定を依頼してください。")
    identity = str(UUID(user_id))
    prefix = f"/tenants/{UUID(str(tenant_id))}/users/{identity}" if tenant_id else f"/users/{identity}"
    try:
        async with httpx.AsyncClient(timeout=55, trust_env=False) as client:
            response = await client.request(method, f"{settings.codex_controller_url}{prefix}{path}",
                headers={"Authorization": "Bearer " + settings.codex_controller_token.get_secret_value()}, json=body)
        if response.status_code == 409:
            raise HTTPException(409, notice(response))
        if response.status_code == 404:
            raise HTTPException(404, "生成履歴が見つかりません。")
        if response.status_code == 503:
            raise HTTPException(503, notice(response))
        response.raise_for_status()
        return response.json()
    except (httpx.HTTPError, ValueError):
        raise HTTPException(503, "AppGenの実行環境を準備中、または接続できません。少し待って再度お試しください。") from None


async def controller_system(settings, method, path, body=None, timeout=420):
    """Fixed controller-wide operations used only by platform administration."""
    tenant_settings = re.fullmatch(r"/settings/tenants/[0-9a-f-]{36}/llm", path)
    if not settings.codex_controller_url or (not tenant_settings and path not in {
            "/migrations", "/migrations/cleanup", "/migrations/legacy-claim",
            "/support/revoke", "/settings/gemini", "/settings/antigravity",
            "/settings/openai-compatible", "/settings/claude"}):
        raise HTTPException(503, "AppGenの実行環境が未設定です。")
    try:
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
            response = await client.request(method, settings.codex_controller_url + path,
                headers={"Authorization": "Bearer " + settings.codex_controller_token.get_secret_value()},
                json=body)
        response.raise_for_status()
        return response.json()
    except httpx.HTTPError:
        message = ("サポート用実行環境を終了できませんでした。権限は失効しています。"
                   if path == "/support/revoke" else
                   "テナント移行を完了できませんでした。移行元は変更していません。")
        raise HTTPException(503, message) from None


async def tenant_storage(settings, tenant_id):
    """Measure one trusted tenant claim; the controller accepts no arbitrary path."""
    if not settings.codex_controller_url:
        raise HTTPException(503, "AppGenの実行環境が未設定です。")
    tenant = str(UUID(str(tenant_id)))
    try:
        async with httpx.AsyncClient(timeout=90, trust_env=False) as client:
            response = await client.get(f"{settings.codex_controller_url}/tenants/{tenant}/storage",
                headers={"Authorization": "Bearer " + settings.codex_controller_token.get_secret_value()})
        response.raise_for_status()
        return response.json()
    except (httpx.HTTPError, ValueError):
        raise HTTPException(503, "生成領域の使用量を取得できませんでした。") from None
