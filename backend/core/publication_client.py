"""Fixed operations to a dedicated publication controller; no Kubernetes credentials here."""
import httpx
from fastapi import HTTPException


async def call(settings, method, path, payload=None, *, timeout=45):
    if not settings.publication_enabled:
        raise HTTPException(503, 'アプリの公開機能は無効です。管理者に設定を依頼してください。')
    try:
        async with httpx.AsyncClient(timeout=timeout, trust_env=False, follow_redirects=False) as client:
            response = await client.request(method, settings.publication_controller_url + path,
                headers={'Authorization': 'Bearer ' + settings.publication_controller_token.get_secret_value()},
                json=payload)
    except httpx.HTTPError:
        raise HTTPException(503, '公開基盤へ接続できません。状態を更新して確認してください。') from None
    if response.status_code == 422:
        detail = response.json().get('detail', '')
        # Only known operator guidance is propagated; arbitrary upstream exception text is hidden.
        prefixes = ('ノードのPull設定', 'Registryのノード同期基盤', '内部RegistryのHTTP/TLS設定',
                    '公開PVC')
        if isinstance(detail, str) and detail.startswith(prefixes):
            raise HTTPException(422, detail[:300])
    if response.status_code == 409:
        raise HTTPException(409, '処理中の操作があります。状態を更新してからやり直してください。')
    if response.status_code == 404:
        if path.startswith('/registry/'):
            raise HTTPException(503, '公開コントローラーがこの設定APIに対応していません。管理アプリと同じ版へ更新してください。')
        return None
    if method == 'DELETE' and path.endswith('/data') and response.status_code == 503:
        raise HTTPException(503, '公開データの削除を確認できません。しばらく待って再試行してください。')
    if not response.is_success:
        raise HTTPException(503, '公開基盤で処理を完了できません。管理者に設定・容量を確認してください。')
    return response.json()
