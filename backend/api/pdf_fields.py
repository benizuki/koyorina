import asyncio
import base64
from uuid import uuid4
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session
from backend.core.auth import actor, get_db
from backend.core.db import Audit
from backend.core import gemini_client
from backend.core.gemini_pdf import extract_pdf, extract_pdfs
from backend.domain.pdf_fields import (MAX_PDF_BYTES, MAX_PDF_FILES,
                                       MAX_PDF_TOTAL_BYTES, validate_pdf)

router = APIRouter(prefix="/api/pdf-fields")


def require_gemini(settings):
    if not gemini_client.available(settings) or not gemini_client.model(settings):
        raise HTTPException(503, "PDF抽出に使うGeminiが設定されていません。システム設定で接続先を設定してください。")


class PdfDocument(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    content: str = Field(min_length=1, max_length=MAX_PDF_BYTES * 2)

    @field_validator("name")
    @classmethod
    def safe_name(cls, value):
        if any(ord(character) < 32 for character in value):
            raise ValueError("PDFの名前を確認してください。")
        return value

    def decoded(self):
        try:
            return base64.b64decode(self.content, validate=True)
        except ValueError:
            raise ValueError(f"{self.name}を読み取れません。") from None


class PdfBatch(BaseModel):
    files: list[PdfDocument] = Field(min_length=1, max_length=MAX_PDF_FILES)


async def run_extraction(request, db, user, documents, extractor):
    settings = request.app.state.settings
    lock = request.app.state.pdf_extraction_lock
    if lock.locked():
        raise HTTPException(429, "別のPDFを処理中です。少し待ってからお試しください。")
    async with lock:
        attempt = str(uuid4())
        db.add(Audit(actor_id=user.id, action="pdf.extraction.requested", resource_id=attempt))
        db.commit()
        try:
            async with asyncio.timeout(65):
                result = await extractor(documents, settings)
        except Exception:
            db.add(Audit(actor_id=user.id, action="pdf.extraction.failed", resource_id=attempt))
            db.commit()
            raise HTTPException(502, "項目を抽出できませんでした。PDFの判読状態、Gemini APIの認証・モデル設定を確認してください。") from None
        db.add(Audit(actor_id=user.id, action="pdf.extraction.completed", resource_id=attempt))
        db.commit()
        return result.model_dump()


@router.post("/batch")
async def extract_batch(payload: PdfBatch, request: Request, db: Session = Depends(get_db)):
    user = actor(request, db)
    settings = request.app.state.settings
    if not settings.pdf_extraction_enabled:
        raise HTTPException(503, "PDF抽出はGemini APIの接続設定待ちです。項目は手入力できます。")
    require_gemini(settings)
    if request.headers.get("x-pdf-consent") != "yes":
        raise HTTPException(400, "PDFを設定済みのGemini APIへ送信することを確認してください。")
    try:
        documents = [item.decoded() for item in payload.files]
        if sum(len(data) for data in documents) > MAX_PDF_TOTAL_BYTES:
            raise ValueError("PDFの合計は15MiB以下にしてください。")
        for data in documents:
            validate_pdf(data)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    return await run_extraction(request, db, user, documents, extract_pdfs)


@router.post("")
async def extract(request: Request, db: Session = Depends(get_db)):
    user = actor(request, db)  # 読込・外部送信より先に招待済み/停止状態を確認
    settings = request.app.state.settings
    if not settings.pdf_extraction_enabled:
        raise HTTPException(503, "PDF抽出はGemini APIの接続設定待ちです。項目は手入力できます。")
    require_gemini(settings)
    if request.headers.get("x-pdf-consent") != "yes":
        raise HTTPException(400, "PDFを設定済みのGemini APIへ送信することを確認してください。")
    # 小規模初期版は1プロセス1件まで。同時実行によるメモリ・課金増幅を抑える。
    lock = request.app.state.pdf_extraction_lock
    if lock.locked():
        raise HTTPException(429, "別のPDFを処理中です。少し待ってからお試しください。")
    async with lock:
        data = bytearray()
        try:
            async with asyncio.timeout(30):
                async for chunk in request.stream():
                    if len(data) + len(chunk) > MAX_PDF_BYTES:
                        raise HTTPException(413, "PDFは5MiB以下にしてください。")
                    data.extend(chunk)
        except TimeoutError:
            raise HTTPException(408, "PDFの送信に時間がかかっています。再度お試しください。") from None
        try:
            validate_pdf(data)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        attempt = str(uuid4())
        db.add(Audit(actor_id=user.id, action="pdf.extraction.requested", resource_id=attempt))
        db.commit()
        try:
            async with asyncio.timeout(65):
                result = await extract_pdf(bytes(data), settings)
        except Exception:
            db.add(Audit(actor_id=user.id, action="pdf.extraction.failed", resource_id=attempt))
            db.commit()
            # SDK例外には資料や認証情報が混ざるため出力しない。
            raise HTTPException(502, "項目を抽出できませんでした。PDFの判読状態、Gemini APIの認証・モデル設定を確認してください。") from None
        finally:
            data.clear()
        db.add(Audit(actor_id=user.id, action="pdf.extraction.completed", resource_id=attempt))
        db.commit()
        return result.model_dump()
