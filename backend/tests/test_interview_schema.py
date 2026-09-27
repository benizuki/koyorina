import asyncio
from types import SimpleNamespace
import pytest
from pydantic import ValidationError
from backend.domain import interview
from backend.domain.interview import GeminiInterviewDecision, gemini_response_schema
from backend.domain.projects import ProjectInput
from backend.worker.gemini_agent import structured_interview

SPEC = ProjectInput(name="Requests", purpose="Manage internal requests",
                    fields=[{"name": "title", "kind": "text"}])
QUESTION = {"id": "approval", "header": "Approval", "question": "Need approval?",
            "options": [{"label": "Yes", "description": "Require approval"},
                        {"label": "No", "description": "No approval"}]}


def test_wire_schema_keeps_shape_without_complex_bounds():
    from backend.domain.pdf_fields import ExtractedFields
    from backend.domain.purpose_draft import PurposeDraft
    # extra="forbid" のモデルは additionalProperties を持つ。そのまま送ると
    # Gemini API（AI Studio）が 400 INVALID_ARGUMENT で断る。
    for model in (GeminiInterviewDecision, ProjectInput, PurposeDraft, ExtractedFields):
        wire = gemini_response_schema(model)
        def check(value):
            if isinstance(value, dict):
                assert not ({"minItems", "maxItems", "minLength", "maxLength", "pattern",
                             "additionalProperties", "default"} & value.keys())
                for item in value.values(): check(item)
            elif isinstance(value, list):
                for item in value: check(item)
        check(wire)
        assert wire["required"] == model.model_json_schema()["required"]
        assert wire["properties"].keys() == model.model_json_schema()["properties"].keys()
    assert gemini_response_schema(GeminiInterviewDecision)["properties"]["action"]["enum"] == ["ask", "complete"]


def invoke(response, answers=None):
    captured, closed = [], []
    async def generate(**kwargs):
        captured.append(kwargs["config"])
        return response
    async def close(): closed.append(True)
    client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=generate), aclose=close))
    try:
        return asyncio.run(structured_interview("test-model", SPEC, answers,
                                               client_factory=lambda: client)), captured
    finally:
        assert closed == [True]


def test_the_interview_asks_about_what_is_expensive_to_change_later():
    """初版に不可欠な判断だけを聞き、学習を始めるまでの負担を抑える。"""
    from backend.worker.gemini_agent import INTERVIEW_INSTRUCTION as text
    for topic in ("主な利用者", "作業の流れ", "状態", "計算方法", "1日の開始時刻"):
        assert topic in text, topic
    # 逆に、聞いてはいけないことは聞かせない。
    for skip in ("ログイン", "置き場所", "見た目"):
        assert skip in text, skip
    assert "最大2回" in text and "最大3問" in text and "2回や3問を使い切る必要はありません" in text


def test_the_round_limit_forces_a_finish(tmp_path):
    """聞くことが尽きても質問が続く状態を作らない。"""
    from backend.domain.interview import MAX_ROUNDS, MAX_OPTIONS, MAX_QUESTIONS
    assert (MAX_ROUNDS, MAX_QUESTIONS, MAX_OPTIONS) == (2, 3, 4)


def test_questions_and_answered_spec_are_validated():
    result, configs = invoke(SimpleNamespace(parsed={"action": "ask", "questions": [QUESTION]}))
    assert result.action == "ask"
    assert configs[0].automatic_function_calling.disable is True
    result, _ = invoke(SimpleNamespace(parsed=None, text=SPEC.model_dump_json()), answers={"approval": "Yes"})
    assert result == SPEC


@pytest.mark.parametrize("payload", [
    {"action": "ask", "questions": []},
    {"action": "ask", "questions": [{**QUESTION, "options": QUESTION["options"][:1]}]},
    {"action": "complete", "result": None},
    {"action": "ask", "questions": [QUESTION], "unexpected": True},
])
def test_full_validation_still_rejects_invalid_responses(payload):
    with pytest.raises(ValidationError):
        invoke(SimpleNamespace(parsed=payload))


def test_answered_spec_still_rejects_duplicate_fields():
    payload = SPEC.model_dump()
    payload["fields"] *= 2
    with pytest.raises(ValidationError):
        invoke(SimpleNamespace(parsed=payload), answers={"approval": "Yes"})


class ClientError(Exception):
    """google-genai の例外の形だけを真似る（code と本文を持つ）。"""
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def test_quota_is_not_reported_as_a_plain_failure():
    """枠切れは待てば通る。「もう一度」と案内すると、同じ壁に当たり直すだけ。

    google-genai は枠切れも要求の誤りも同じ ClientError で投げるので、
    型名では見分けられない。状態コードで分ける。
    """
    quota = ClientError(429, "RESOURCE_EXHAUSTED: quota exceeded for model")
    assert interview.interview_failure_code(quota) == "quota"
    assert "利用枠" in interview.INTERVIEW_ERRORS["quota"]

    wrong = ClientError(400, "INVALID_ARGUMENT: response_schema is too deep")
    assert interview.interview_failure_code(wrong) == "failed"


def test_temporary_provider_congestion_has_a_clear_message():
    class ServerError(Exception):
        code = 503

    class ProviderUnavailable(Exception):
        pass

    assert interview.interview_failure_code(ServerError("high demand")) == "provider_busy"
    assert interview.interview_failure_code(ProviderUnavailable()) == "provider_busy"
    assert "混み合っています" in interview.INTERVIEW_ERRORS["provider_busy"]


def test_the_reason_is_recorded_without_the_specification():
    """例外文には利用者の入力が入りうる。記録へそのまま流さない。"""
    leaky = ClientError(400, "INVALID_ARGUMENT: 見積書の宛名は必須 / project=secret-co")
    assert interview.redacted_reason(leaky) == "INVALID_ARGUMENT"
    assert interview.failure_status(leaky) == 400
    # 合図が無ければ型名だけ。本文は出さない。
    assert interview.redacted_reason(ValueError("宛名が読めません")) == "ValueError"
    assert interview.failure_status(ValueError("x")) is None


def test_the_final_step_waits_out_a_quota_wall():
    """回答し終えた直後の「整理」だけが落ちる、を無くす。

    ここで諦めると、ヒアリングの最初からやり直しになる。答えた内容が無駄に
    なるので、いちばん避けたい落ち方。生成と同じように待ち直す。
    """
    from backend.worker import gemini_agent
    from backend.domain.projects import ProjectInput

    attempts, waited = [], []

    class Models:
        async def generate_content(self, **kwargs):
            attempts.append(1)
            if len(attempts) < 3:
                raise ClientError(429, "RESOURCE_EXHAUSTED")
            return SimpleNamespace(parsed=None, text=ProjectInput(
                name="台帳", purpose="貸出を管理する", audience="team",
                fields=[{"name": "品名", "kind": "text", "required": True}]).model_dump_json())

    client = SimpleNamespace(aio=SimpleNamespace(models=Models(), aclose=None))
    spec = ProjectInput(name="台帳", purpose="貸出を管理する", audience="team",
                        fields=[{"name": "品名", "kind": "text", "required": True}])

    async def sleep(seconds):
        waited.append(seconds)

    result = asyncio.run(gemini_agent.structured_interview(
        "gemini-3.8-flash", spec, answers=[], client_factory=lambda: client,
        final=True, sleep=sleep))
    assert result.name == "台帳"
    assert len(attempts) == 3 and waited == list(gemini_agent.RETRY_DELAYS[:2])


def test_overflowing_questions_are_trimmed_not_thrown_away():
    """上限をスキーマに載せられないので、はみ出しは起きる。意味を変えない範囲で直して受け取る。"""
    many = [{**QUESTION, "id": "Approval 質問", "header": "とても長い見出し" * 5,
             "options": QUESTION["options"] * 3}] * 4
    result, configs = invoke(SimpleNamespace(parsed={"action": "ask", "questions": many}))
    # 1回に出せるのは3問・選択肢4件まで。学習を始める前の負担を増やさない。
    assert len(result.questions) == 3 and len(configs) == 1
    assert [question.id for question in result.questions] == ["q1", "q2", "q3"]
    assert all(len(question.options) == 4 and len(question.header) <= 24 for question in result.questions)


def test_an_invalid_shape_is_asked_again_once_with_only_the_locations(caplog):
    """直せない形の崩れは1回だけ聞き直す。伝えるのは場所と種類だけで、値は送らない・残さない。"""
    secret = "取引先の極秘名"
    responses = iter([SimpleNamespace(parsed={"action": "ask", "questions": [
                          {**QUESTION, "question": secret * 100}]}),
                      SimpleNamespace(parsed={"action": "ask", "questions": [QUESTION]})])
    prompts = []

    async def generate(**kwargs):
        prompts.append(kwargs["contents"])
        return next(responses)

    async def close():
        pass
    client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=generate), aclose=close))
    with caplog.at_level("WARNING"):
        result = asyncio.run(structured_interview("test-model", SPEC, None, client_factory=lambda: client))
    assert result.action == "ask" and len(prompts) == 2
    assert "questions.0.question:string_too_long" in prompts[1]
    assert secret not in prompts[1].split("前回の回答は")[1]
    assert "questions.0.question:string_too_long" in caplog.text and secret not in caplog.text


def test_numeric_choices_do_not_reach_the_sdk_as_an_enum():
    """SDKは enum に文字列しか受け付けない。数値の選択肢で送る前に落ちていた。"""
    from google.genai import Client, _transformers
    # 送る直前にSDKが dict を Schema へ変換する（$ref の展開を含む）。ここで ValidationError になっていた。
    api = Client(api_key="test-only-not-a-real-key")._api_client
    for model in (GeminiInterviewDecision, ProjectInput):
        assert _transformers.t_schema(api, gemini_response_schema(model)) is not None
    # 値の検査は受け取った後で行う。
    with pytest.raises(ValidationError):
        ProjectInput(**SPEC.model_dump(), creation_profile={"app_pattern": "local_file_visualization",
                                                             "production_day_hours": 10})
