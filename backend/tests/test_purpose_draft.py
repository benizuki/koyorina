import asyncio
from types import SimpleNamespace

from backend.domain.purpose_draft import PurposeDraftInput, purpose_context
from backend.tests.test_api import context, login  # noqa: F401


def payload():
    return {
        "name": "生産実績ビューア",
        "creation_profile": {
            "app_pattern": "local_file_visualization",
            "work_category": "other",
            "app_type": "records",
            "goals": ["production_performance"],
            "production_day_start": "06:00",
            "sample_data": {
                "filename": "secret.csv", "sheet": None, "row_count": 10,
                "columns": [{"name": "実績数", "kind": "number",
                             "samples": ["社外へ送らない値"], "suggested_role": "quantity"}],
                "warnings": [],
            },
            "column_mappings": [{"column": "実績数", "role": "quantity"}],
        },
        "tables": [{"name": "取込データ", "kind": "record",
                    "fields": [{"name": "実績数", "kind": "number", "required": False}]}],
    }


def test_purpose_context_uses_confirmed_shape_without_sample_values():
    context = purpose_context(PurposeDraftInput.model_validate(payload()))
    assert "生産実績ビューア" in context and "実績推移" in context
    assert "実績数" in context and "quantity" in context
    assert "secret.csv" not in context
    assert "社外へ送らない値" not in context


def test_purpose_draft_reserves_output_for_json(monkeypatch):
    from backend.core.gemini_purpose import draft_purpose

    captured = {}

    class Models:
        async def generate_content(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(parsed={"purpose": "担当者が実績推移を確認するために使います。"})

    class AsyncClient:
        async def __aenter__(self):
            return SimpleNamespace(models=Models())

        async def __aexit__(self, *_):
            return None

    monkeypatch.setattr("backend.core.gemini_purpose.client",
                        lambda *_args, **_kwargs: SimpleNamespace(aio=AsyncClient()))
    settings = SimpleNamespace(vertex_model="test-model")

    result = asyncio.run(draft_purpose(PurposeDraftInput.model_validate(payload()), settings))

    assert result.purpose.startswith("担当者が")
    assert captured["config"].max_output_tokens == 1024
    assert captured["config"].thinking_config.thinking_budget == 0


def test_purpose_draft_endpoint_returns_editable_text(context, monkeypatch):
    from backend.domain.purpose_draft import PurposeDraft

    client, _ = context
    login(client)
    client.app.state.settings.vertex_model = "test-model"
    monkeypatch.setattr("backend.api.projects.gemini_available", lambda _: True)

    async def fake_draft(value, settings):
        assert value.name == "生産実績ビューア"
        assert settings.vertex_model == "test-model"
        return PurposeDraft(purpose="製造担当者が日々の生産実績を確認し、進捗を把握するために使います。")

    monkeypatch.setattr("backend.api.projects.draft_purpose", fake_draft)
    response = client.post("/api/projects/purpose-draft", json=payload())
    assert response.status_code == 200, response.text
    assert response.json()["purpose"].startswith("製造担当者が")
