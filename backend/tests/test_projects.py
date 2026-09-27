import pytest
from pydantic import ValidationError
from backend.domain.projects import ProjectInput, approve, specification
from backend.domain.generation import generation_prompt, manifest_prompt
from backend.config.settings import Settings
from backend.core.codex_bridge import isolated_environment
from uuid import uuid4

INPUT = {"name": "備品管理", "purpose": "備品の貸出を管理する", "audience": "team", "fields": [{"name": "備品名", "kind": "text", "required": True}]}


def test_valid_spec():
    assert ProjectInput(**INPUT).fields[0].name == "備品名"


def test_interview_requirements_are_preserved():
    spec = ProjectInput(**INPUT, requirements=[" 担当者と管理者だけが編集できる。 "])
    assert spec.requirements == ["担当者と管理者だけが編集できる。"]


def test_user_can_override_generation_prompt():
    request = "備品の返却遅れが分かる一覧を最初に作ってください。"
    spec = ProjectInput(**INPUT, generation_prompt=request)
    assert request in generation_prompt(spec)
    assert request in manifest_prompt(spec)
    # 利用者の文章は専用の区画へ一度だけ入れ、仕様JSONへ重複させない。
    assert generation_prompt(spec).count(request) == 1


def test_visualization_profile_preserves_shift_and_confirmed_mapping():
    spec = ProjectInput(**INPUT, creation_profile={
        "work_category": "manufacturing", "app_type": "visualization",
        "goals": ["production_progress", "yield", "defect_pareto"],
        "production_day_start": "06:00",
        "column_mappings": [{"column": "時刻", "role": "timestamp"},
                            {"column": "出来高", "role": "quantity"}],
    })
    assert spec.creation_profile.production_day_start == "06:00"
    assert spec.creation_profile.column_mappings[1].role == "quantity"
    prompt = generation_prompt(spec)
    assert "Compute cumulative production and yield from source rows" in prompt
    assert "one main chart" in prompt and "two to four compact summary cards" in prompt
    assert "without a marketing slogan" in prompt and "application bar" in prompt
    assert "No user management, no roles" not in prompt


def test_visualization_profile_carries_operating_hours_and_initial_period():
    """1日の稼働時間と、開いたときの期間を仕様として渡す。直近はデータの最新日から数える。"""
    from backend.domain.generation import user_generation_prompt
    spec = ProjectInput(**INPUT, creation_profile={
        "app_pattern": "local_file_visualization", "goals": ["production_performance"],
        "production_day_start": "06:00", "production_day_hours": 8, "default_period": "last_7_days"})
    assert spec.creation_profile.production_day_hours == 8
    summary = user_generation_prompt(spec)
    assert "1日の稼働時間: 8時間" in summary and "初期表示の期間: 直近7日間" in summary
    prompt = generation_prompt(spec)
    assert "production_day_hours" in prompt and "時間外" in prompt
    assert "latest production day in the loaded file (not from today)" in prompt
    for bad in ({"production_day_hours": 10}, {"default_period": "last_2_days"}):
        with pytest.raises(ValidationError):
            ProjectInput(**INPUT, creation_profile={"app_pattern": "local_file_visualization", **bad})


def test_local_file_visualization_uses_only_a_static_web_server():
    spec = ProjectInput(**INPUT, creation_profile={
        "app_pattern": "local_file_visualization",
        "work_category": "manufacturing", "app_type": "records",
        "goals": ["production_progress"], "production_day_start": "06:00",
    })
    # 3パターンがapp_typeと食い違って送られても、選んだ作り方を正とする。
    assert spec.creation_profile.app_type == "visualization"
    assert spec.creation_profile.work_category == "manufacturing"
    output = specification(spec)
    assert "業務API・SQLAlchemy・データベースは使用しない" in output["stack"]
    assert "サーバーへ送信・保存しない" in output["storage"]
    prompt = generation_prompt(spec)
    assert "Never upload file contents" in prompt
    assert "Do not create business API routes" in prompt
    assert "single main chart" in prompt and "two to four compact summary cards" in prompt
    assert "do not bundle sample files" in prompt
    dependencies = manifest_prompt(spec)
    assert "static SPA web server only" in dependencies
    assert "do not add SQLAlchemy" in dependencies


@pytest.mark.parametrize(("pattern", "category"), [
    ("local_file_visualization", "manufacturing"),
    ("data_management", "indirect"),
    ("file_import", "other"),
])
def test_first_choice_determines_work_category(pattern, category):
    spec = ProjectInput(**INPUT, creation_profile={
        "app_pattern": pattern, "work_category": "other", "goals": [],
    })
    assert spec.creation_profile.work_category == category


def test_old_app_type_is_mapped_to_one_of_the_three_patterns():
    visual = ProjectInput(**INPUT, creation_profile={
        "work_category": "other", "app_type": "visualization", "goals": ["file_visualization"]})
    imported = ProjectInput(**INPUT, creation_profile={
        "work_category": "other", "app_type": "both", "goals": ["reporting"]})
    assert visual.creation_profile.app_pattern == "local_file_visualization"
    assert imported.creation_profile.app_pattern == "file_import"


@pytest.mark.parametrize("change", [{"name": " "}, {"audience": ""}, {"fields": []}, {"fields": [{"name": "a"}, {"name": "A"}]}, {"fields": [{"name": "項目", "kind": "sql"}]}])
def test_invalid_spec(change):
    with pytest.raises(ValidationError):
        ProjectInput(**(INPUT | change))


def test_stale_approval():
    with pytest.raises(ValueError):
        approve(2, 1, "draft")
    approve(2, 2, "draft")


def test_production_fails_closed():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, database_url="postgresql+psycopg://localhost/forge")


def test_local_rejected_on_cloud(monkeypatch):
    monkeypatch.setenv("K_SERVICE", "forge")
    with pytest.raises(ValidationError):
        Settings(_env_file=None, app_env="local", database_url="postgresql+psycopg://localhost/forge")


def test_no_api_key_or_existing_auth_inherited(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-secret")
    monkeypatch.setenv("CODEX_API_KEY", "test-secret")
    monkeypatch.setenv("CODEX_ACCESS_TOKEN", "test-secret")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "test-secret")
    monkeypatch.setenv("CODEX_HOME", "/existing/account")
    first, env = isolated_environment(tmp_path, str(uuid4()))
    second, _ = isolated_environment(tmp_path, str(uuid4()))
    assert first != second
    assert not any(k in env for k in ("OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN", "GOOGLE_APPLICATION_CREDENTIALS"))
    assert env["CODEX_HOME"] != "/existing/account"
    assert first.stat().st_mode & 0o777 == 0o700


def test_path_traversal_rejected(tmp_path):
    with pytest.raises(ValueError):
        isolated_environment(tmp_path, "../../another-user")



def test_audience_is_no_longer_asked_and_defaults_to_team():
    """利用者は聞かない。社内の招待制が前提のため、チーム共有で固定する。"""
    spec = ProjectInput(name="会議室予約", purpose="予約状況を記録する",
                        fields=[{"name": "会議室名", "kind": "text", "required": True}])
    assert spec.audience == "team"
    # 既存データの値は保てる。
    assert ProjectInput(name="個人メモ", purpose="自分用に記録する", audience="self",
                        fields=[{"name": "件名", "kind": "text", "required": True}]).audience == "self"


def test_prompt_mode_makes_the_request_the_primary_specification():
    request = "問い合わせの受付と対応状況を管理したい。担当者ごとに一覧で見たい。"
    spec = ProjectInput(name="問い合わせ管理", purpose="", fields=[], generation_prompt=request,
                        creation_profile={"app_pattern": "data_management", "mode": "prompt"})
    prompt = generation_prompt(spec)
    assert request in prompt
    assert "primary specification" in prompt
    assert "primary specification" in manifest_prompt(spec)
    assert '"mode":"prompt"' in prompt


def test_guided_profile_json_is_unchanged_by_prompt_mode():
    spec = ProjectInput(**INPUT, creation_profile={"app_pattern": "data_management"})
    assert "mode" not in spec.model_dump_json()
    assert "primary specification" not in generation_prompt(spec)


def test_ai_processing_tells_the_generator_to_read_files_with_gemini():
    spec = ProjectInput(name="請求書の読取", purpose="PDFの請求書を読み取って登録する", audience="team",
                        fields=[{"name": "取引先", "kind": "text", "required": True}],
                        creation_profile={"app_pattern": "file_import", "goals": ["records", "ai_processing"]})
    prompt = generation_prompt(spec)
    assert "AI処理（Geminiで読み取り・分析）" in prompt
    assert "types.Part.from_bytes" in prompt and "response_schema" in prompt
    assert "confirmation" in prompt and "llm.available" in prompt
    # CSVの見本が無くても、依頼にあるファイル（PDFなど）を取り込む前提で作らせる。
    assert "When `sample_data` is absent" in prompt
    assert "google-genai" in manifest_prompt(spec)


def test_without_ai_processing_nothing_about_gemini_is_added():
    spec = ProjectInput(**INPUT, creation_profile={"app_pattern": "file_import", "goals": ["reporting"]})
    assert "types.Part.from_bytes" not in generation_prompt(spec)
    assert "google-genai" not in manifest_prompt(spec)
