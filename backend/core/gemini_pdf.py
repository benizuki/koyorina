from google.genai import types
from backend.core.gemini_client import client, model, thinking_config
from backend.domain.interview import gemini_response_schema
from backend.domain.pdf_fields import (ExtractedFields, EXTRACTION_INSTRUCTION,
                                       MAX_PDF_FILES, MAX_PDF_TOTAL_BYTES, validate_pdf)


async def extract_pdf(data, settings):
    return await extract_pdfs([data], settings)


async def extract_pdfs(documents, settings):
    if not 1 <= len(documents) <= MAX_PDF_FILES:
        raise ValueError(f"PDFは{MAX_PDF_FILES}件まで選べます。")
    if sum(len(data) for data in documents) > MAX_PDF_TOTAL_BYTES:
        raise ValueError("PDFの合計は15MiB以下にしてください。")
    for data in documents:
        validate_pdf(data)
    async with client(settings, timeout=60_000, attempts=3).aio as api:
        response = await api.models.generate_content(
            model=model(settings),
            contents=[types.Part.from_bytes(data=data, mime_type="application/pdf")
                      for data in documents],
            config=types.GenerateContentConfig(
                system_instruction=EXTRACTION_INSTRUCTION,
                # 形だけを送る（gemini_purpose と同じ理由）。制約は受け取ってから検査する。
                response_mime_type="application/json",
                response_schema=gemini_response_schema(ExtractedFields),
                temperature=0, max_output_tokens=4096, thinking_config=thinking_config(settings),
                automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            ),
        )
    # 構造化出力ではSDKがparsedだけを返すことがある。どちらの経路も同じ型で検証する。
    parsed = getattr(response, "parsed", None)
    if parsed is not None:
        return ExtractedFields.model_validate(parsed)
    # SDKの値を無条件に信用しない。空・壊れたJSON・不正型は拒否する。
    return ExtractedFields.model_validate_json(response.text or "")
