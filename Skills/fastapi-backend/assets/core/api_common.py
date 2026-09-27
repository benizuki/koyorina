"""api/ の各ルータで共通に使う小さなヘルパ。"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse


BACKEND_DIR = Path(__file__).resolve().parents[1]
FRONTEND_DIST_DIR = (BACKEND_DIR.parent / "frontend" / "dist").resolve()


def _json_error(message: str, status_code: int) -> JSONResponse:
    """エラーレスポンスの形を 1 つに揃える。フロント側の分岐が減る。"""
    return JSONResponse(status_code=status_code, content={"error": message})


async def _request_json(request: Request) -> dict:
    """リクエスト本文を dict として読む。

    Pydantic のモデルを使わずこの形にしているのは、部分更新（PATCH 的な PUT）を
    受けるエンドポイントが多く、「未指定」と「null 指定」を区別したいため。
    入力の型が固まっている API では、素直に Pydantic モデルを使ってよい。
    """
    body = await request.body()
    if not body:
        return {}
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"JSON が不正です: {exc.msg}") from exc
    return payload if isinstance(payload, dict) else {}


def _frontend_index() -> FileResponse:
    index_file = FRONTEND_DIST_DIR / "index.html"
    if not index_file.exists():
        raise HTTPException(
            status_code=404,
            detail="frontend のビルド成果物が見つかりません。frontend で npm run build を実行してください。",
        )
    return FileResponse(index_file)
