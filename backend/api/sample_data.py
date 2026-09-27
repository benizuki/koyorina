import base64
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from backend.core.auth import actor, get_db
from backend.core.db import Audit
from backend.domain.sample_data import MAX_SAMPLE_BYTES, analyze_sample


router = APIRouter(prefix="/api/sample-data")


class SampleDocument(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    content: str = Field(min_length=1, max_length=MAX_SAMPLE_BYTES * 2)

    @field_validator("name")
    @classmethod
    def safe_name(cls, value):
        if any(ord(character) < 32 for character in value):
            raise ValueError("ファイル名を確認してください。")
        return value


@router.post("/analyze")
def analyze(payload: SampleDocument, request: Request, db: Session = Depends(get_db)):
    user = actor(request, db)
    try:
        data = base64.b64decode(payload.content, validate=True)
        result = analyze_sample(payload.name, data)
    except (ValueError, base64.binascii.Error) as exc:
        raise HTTPException(422, str(exc) or "ファイルを読み取れませんでした。") from None
    attempt = str(uuid4())
    db.add(Audit(actor_id=user.id, action="sample_data.analyzed", resource_id=attempt,
                 detail=f"columns={len(result.columns)}; rows={result.row_count}"))
    db.commit()
    return result.model_dump()
