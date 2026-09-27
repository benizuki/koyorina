"""モデルを実行せず、独立環境でApp Serverのハンドシェイクを確認する。"""
import asyncio
import os
from pathlib import Path
from uuid import uuid4
from backend.core.codex_bridge import CodexBridge


async def main():
    # DB等の設定は要らない。Codexの場所と、空の検証環境を作る場所だけ。
    binary = os.environ.get("CODEX_BINARY", "codex")
    root = Path(os.environ.get("RUNTIME_ROOT", ".runtime"))
    async with CodexBridge(binary, root / "auth-probes", str(uuid4())) as bridge:
        result = await bridge.call("account/read", {"refreshToken": False})
        account = result.get("account")
        if account is not None:
            raise RuntimeError("空の環境に予期しない認証があります。連携を停止してください。")
        print("App Server接続成功。独立環境は未認証。APIキー・既存アカウントの継承なし。モデル実行なし。")


if __name__ == "__main__":
    asyncio.run(main())
