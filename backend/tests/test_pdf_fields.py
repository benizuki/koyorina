import pytest
import base64
from pydantic import ValidationError
from sqlalchemy import select
from backend.tests.test_api import context, login
from backend.domain.pdf_fields import (ExtractedFields, MAX_PDF_BYTES,
                                       MAX_PDF_FILES, MAX_PDF_TOTAL_BYTES, validate_pdf)
from backend.core.db import Audit

PDF = b"%PDF-1.7\nmock test document\n%%EOF"
HEADERS = {"Content-Type": "application/pdf", "X-PDF-Consent": "yes"}


def test_pdf_validation():
    validate_pdf(PDF)
    for content in (b"", b"not a pdf", b"%PDF-1.7 incomplete", b"x" * (MAX_PDF_BYTES + 1)):
        with pytest.raises(ValueError):
            validate_pdf(content)


def test_candidate_validation():
    with pytest.raises(ValidationError):
        ExtractedFields(fields=[])
    with pytest.raises(ValidationError):
        ExtractedFields(fields=[{"name": "金額", "kind": "code"}])
    with pytest.raises(ValidationError):
        ExtractedFields(fields=[{"name": "金額"}, {"name": "金額"}])


def test_pdf_auth_disabled_and_csrf(context):
    client, _ = context
    assert client.post("/api/pdf-fields", content=PDF, headers=HEADERS).status_code == 401
    login(client)
    assert client.post("/api/pdf-fields", content=PDF, headers=HEADERS).status_code == 503
    assert client.post("/api/pdf-fields", content=PDF, headers=HEADERS | {"Origin": "https://evil.test"}).status_code == 403
    assert client.post("/api/pdf-fields", json={}).status_code == 415


def enable(client):
    # テスト専用。実際のGCP送信はモックする。
    client.app.state.settings.pdf_extraction_enabled = True
    client.app.state.settings.vertex_project = "example-project-123"
    client.app.state.settings.vertex_location = "global"
    client.app.state.settings.vertex_model = "gemini-test-model"


def test_pdf_candidates_do_not_mutate_projects(context, monkeypatch):
    client, sessions = context
    login(client); enable(client)
    before = client.get("/api/projects").json()
    async def fake(data, settings):
        assert data == PDF
        return ExtractedFields(fields=[{"name": "金額", "kind": "number", "required": False}], warnings=["必須か確認してください"])
    monkeypatch.setattr("backend.api.pdf_fields.extract_pdf", fake)
    assert client.post("/api/pdf-fields", content=PDF, headers={"Content-Type": "application/pdf"}).status_code == 400
    response = client.post("/api/pdf-fields", content=PDF, headers=HEADERS)
    assert response.status_code == 200
    assert response.json()["fields"][0]["required"] is False
    with sessions() as db:
        actions = list(db.scalars(select(Audit.action).where(Audit.action.like("pdf.%"))))
        assert actions == ["pdf.extraction.requested", "pdf.extraction.completed"]
    assert client.get("/api/projects").json() == before


def test_multiple_pdfs_are_extracted_in_one_model_request(context, monkeypatch):
    client, _ = context
    login(client); enable(client)
    received = []
    async def fake(documents, settings):
        received.extend(documents)
        return ExtractedFields(fields=[
            {"name": "申請者", "kind": "text", "required": True},
            {"name": "金額", "kind": "number", "required": False},
        ], warnings=["帳票ごとに必須条件を確認してください"])
    monkeypatch.setattr("backend.api.pdf_fields.extract_pdfs", fake)
    payload = {"files": [
        {"name": "申請書.pdf", "content": base64.b64encode(PDF).decode()},
        {"name": "精算書.pdf", "content": base64.b64encode(PDF + b"\n%%EOF").decode()},
    ]}
    response = client.post("/api/pdf-fields/batch", json=payload,
                           headers={"X-PDF-Consent": "yes"})
    assert response.status_code == 200
    assert received == [PDF, PDF + b"\n%%EOF"]
    assert [field["name"] for field in response.json()["fields"]] == ["申請者", "金額"]


def test_pdf_batch_limits_are_enforced(context):
    client, _ = context
    login(client); enable(client)
    encoded = base64.b64encode(PDF).decode()
    too_many = {"files": [{"name": f"{index}.pdf", "content": encoded}
                           for index in range(MAX_PDF_FILES + 1)]}
    assert client.post("/api/pdf-fields/batch", json=too_many,
                       headers={"X-PDF-Consent": "yes"}).status_code == 422
    assert MAX_PDF_TOTAL_BYTES == 15 * 1024 * 1024


def test_invalid_oversize_and_concurrent_pdf(context, monkeypatch):
    client, _ = context
    login(client); enable(client)
    assert client.post("/api/pdf-fields", content=b"not a pdf", headers=HEADERS).status_code == 422
    assert client.post("/api/pdf-fields", content=b"x" * (MAX_PDF_BYTES + 1), headers=HEADERS).status_code == 413
    class Locked:
        def locked(self): return True
    client.app.state.pdf_extraction_lock = Locked()
    assert client.post("/api/pdf-fields", content=PDF, headers=HEADERS).status_code == 429


def test_sdk_failure_is_sanitized(context, monkeypatch):
    client, _ = context
    login(client); enable(client)
    async def fail(data, settings):
        raise RuntimeError("private document credential secret")
    monkeypatch.setattr("backend.api.pdf_fields.extract_pdf", fail)
    response = client.post("/api/pdf-fields", content=PDF, headers=HEADERS)
    assert response.status_code == 502
    assert "secret" not in response.text
    assert not client.app.state.pdf_extraction_lock.locked()


def test_genai_adapter_uses_vertex_and_validated_json(monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from backend.core.gemini_pdf import extract_pdf
    class Models:
        async def generate_content(self, **kwargs):
            assert kwargs["model"] == "test-model"
            assert kwargs["contents"][0].inline_data.data == PDF
            assert kwargs["config"].automatic_function_calling.disable is True
            # 形だけを送る（additionalProperties は Gemini API が断る）。制約は受け取ってから検査する。
            from backend.domain.interview import gemini_response_schema
            assert kwargs["config"].response_schema == gemini_response_schema(ExtractedFields)
            return SimpleNamespace(text='{"fields":[{"name":"金額","kind":"number","required":false}],"warnings":[]}')
    class Client:
        def __init__(self, **kwargs):
            assert kwargs["vertexai"] is True
            assert kwargs["project"] == "test-project"
            assert "api_key" not in kwargs
            self.aio = self
            self.models = Models()
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
    monkeypatch.setattr("backend.core.gemini_client.genai.Client", Client)
    result = asyncio.run(extract_pdf(PDF, SimpleNamespace(
        gemini_api_backend="vertex", gemini_api_key="", vertex_project="test-project",
        vertex_location="test-location", vertex_model="test-model")))
    assert result.fields[0].name == "金額"


def test_genai_adapter_accepts_the_sdks_parsed_structured_result(monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from backend.core.gemini_pdf import extract_pdf

    parsed = {"fields": [{"name": "申請日", "kind": "date", "required": True}], "warnings": []}

    class Models:
        async def generate_content(self, **kwargs):
            retry = kwargs.get("config")
            return SimpleNamespace(text="", parsed=parsed)

    class Client:
        def __init__(self, **kwargs):
            retry = kwargs["http_options"].retry_options
            assert retry.attempts == 3 and retry.max_delay == 4
            self.aio, self.models = self, Models()
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass

    monkeypatch.setattr("backend.core.gemini_client.genai.Client", Client)
    result = asyncio.run(extract_pdf(PDF, SimpleNamespace(
        gemini_api_backend="vertex", gemini_api_key="", vertex_project="test-project",
        vertex_location="global", vertex_model="test-model")))
    assert result.fields[0].name == "申請日"


def test_genai_adapter_uses_google_ai_studio_api_key(monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from backend.core.gemini_pdf import extract_pdf

    class Models:
        async def generate_content(self, **kwargs):
            return SimpleNamespace(text='{"fields":[{"name":"金額","kind":"number","required":false}],"warnings":[]}')

    class Client:
        def __init__(self, **kwargs):
            assert kwargs["api_key"] == "test-api-key"
            assert "vertexai" not in kwargs and "project" not in kwargs and "location" not in kwargs
            assert kwargs["http_options"].api_version == "v1beta"
            self.aio, self.models = self, Models()
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass

    monkeypatch.setattr("backend.core.gemini_client.genai.Client", Client)
    result = asyncio.run(extract_pdf(PDF, SimpleNamespace(
        gemini_api_backend="developer", gemini_api_key="test-api-key",
        vertex_project="", vertex_location="", vertex_model="test-model")))
    assert result.fields[0].name == "金額"
