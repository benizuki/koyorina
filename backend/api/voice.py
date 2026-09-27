"""話した内容を文字にして返す。書き起こしを返すだけで、依頼は送らない。

利用者が文字を見て直してから送れるようにする。聞き間違いをそのまま依頼にしない。
"""
import asyncio
from uuid import uuid4
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from backend.core.auth import actor, get_db
from backend.core.db import Audit
from backend.core.gemini_voice import MAX_AUDIO_BYTES, transcribe, validate_audio
from backend.core import gemini_client
from backend.core.gemini_client import available as gemini_available

router = APIRouter(prefix="/api/voice")


@router.post("")
async def voice(request: Request, db: Session = Depends(get_db)):
    user = actor(request, db)  # 送信より先に招待済み・停止状態を確認する
    settings = request.app.state.settings
    if not (gemini_available(settings) and gemini_client.model(settings)):
        raise HTTPException(503, "音声入力はGemini APIの接続設定待ちです。文字で入力してください。")
    lock = request.app.state.voice_lock
    if lock.locked():
        raise HTTPException(429, "別の音声を処理中です。少し待ってからお試しください。")
    async with lock:
        data = bytearray()
        try:
            async with asyncio.timeout(30):
                async for chunk in request.stream():
                    if len(data) + len(chunk) > MAX_AUDIO_BYTES:
                        raise HTTPException(413, "録音が長すぎます。90秒以内で区切ってください。")
                    data.extend(chunk)
        except TimeoutError:
            raise HTTPException(408, "音声の送信に時間がかかっています。再度お試しください。") from None
        try:
            validate_audio(bytes(data))
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        attempt = str(uuid4())
        db.add(Audit(actor_id=user.id, action="voice.transcription.requested", resource_id=attempt))
        db.commit()
        try:
            async with asyncio.timeout(65):
                text = await transcribe(bytes(data), settings)
        except Exception:
            db.add(Audit(actor_id=user.id, action="voice.transcription.failed", resource_id=attempt))
            db.commit()
            # SDKの例外には音声や認証情報が混ざるため出力しない。
            raise HTTPException(502, "書き起こせませんでした。もう一度話すか、文字で入力してください。") from None
        finally:
            data.clear()
        db.add(Audit(actor_id=user.id, action="voice.transcription.completed", resource_id=attempt))
        db.commit()
        return {"text": text}
