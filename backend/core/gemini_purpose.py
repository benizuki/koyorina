"""Geminiで、利用目的の編集用下書きを生成する。"""
from google.genai import types

from backend.core.gemini_client import client, model
from backend.domain.interview import gemini_response_schema
from backend.domain.purpose_draft import (PURPOSE_DRAFT_INSTRUCTION, PurposeDraft,
                                          PurposeDraftInput, purpose_context)


async def draft_purpose(value: PurposeDraftInput, settings) -> PurposeDraft:
    async with client(settings, timeout=45_000, attempts=2).aio as api:
        response = await api.models.generate_content(
            model=model(settings),
            contents=purpose_context(value),
            config=types.GenerateContentConfig(
                system_instruction=PURPOSE_DRAFT_INSTRUCTION,
                # モデルをそのまま渡すと additionalProperties まで送られ、Gemini API
                # （AI Studio）は400で断る。形だけを送り、制約は受け取ってから検査する。
                response_mime_type="application/json", response_schema=gemini_response_schema(PurposeDraft),
                # 思考トークンで短い出力枠を使い切ると、JSON本文の前で途切れる。
                # この処理は入力済み情報を1文へ整えるだけなので思考は不要。
                temperature=0.2, max_output_tokens=1024,
                thinking_config=types.ThinkingConfig(thinking_budget=0),
                automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            ),
        )
    parsed = getattr(response, "parsed", None)
    if parsed is not None:
        return PurposeDraft.model_validate(parsed)
    return PurposeDraft.model_validate_json(response.text or "")
