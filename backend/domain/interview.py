import asyncio
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from backend.domain.projects import ProjectInput


# 学習用の最初のアプリでは、仕様を詰め切ることより早く作って試せることを優先する。
# 入力済みの作成目的と項目を土台に、初版を作れない判断だけを2回以内で確認する。
MAX_QUESTIONS = 3
MAX_OPTIONS = 4
MAX_ROUNDS = 2


class InterviewOption(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    label: str = Field(min_length=1, max_length=80)
    description: str = Field(min_length=1, max_length=240)


class InterviewQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,39}$")
    header: str = Field(min_length=1, max_length=24)
    question: str = Field(min_length=1, max_length=240)
    options: list[InterviewOption] = Field(min_length=2, max_length=MAX_OPTIONS)
    is_other: bool = True


class GeminiInterviewDecision(BaseModel):
    """A Gemini response either asks the next concise round or completes the specification."""

    model_config = ConfigDict(extra="forbid")
    action: Literal["ask", "complete"]
    questions: list[InterviewQuestion] = Field(default_factory=list, max_length=MAX_QUESTIONS)
    result: ProjectInput | None = None

    @model_validator(mode="after")
    def matching_payload(self):
        if self.action == "ask" and not self.questions:
            raise ValueError("ask requires questions")
        if self.action == "complete" and self.result is None:
            raise ValueError("complete requires a result")
        return self


def normalize_decision(data):
    """回答の形の揺れのうち、意味を変えずに直せるものだけを直す。

    送るスキーマには長さ・件数の上限を載せない（Vertexの複雑さの上限を超えるため）ので、
    モデルは時々上限を超えて返す。1つはみ出しただけで回答全体を捨てると、ヒアリングが
    「読み取れませんでした」で止まる。件数は上限まで、見出しは長さの上限まで切り、
    形式に合わないidは連番に振り直す。中身（仕様の result）には手を付けない。
    """
    if not isinstance(data, dict) or not isinstance(data.get("questions"), list):
        return data
    fixed = dict(data)
    questions = []
    for index, question in enumerate(data["questions"][:MAX_QUESTIONS]):
        if not isinstance(question, dict):
            questions.append(question)
            continue
        question = dict(question)
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,39}", str(question.get("id", ""))):
            question["id"] = f"q{index + 1}"
        if isinstance(question.get("header"), str):
            question["header"] = question["header"].strip()[:24]
        if isinstance(question.get("options"), list):
            question["options"] = question["options"][:MAX_OPTIONS]
        questions.append(question)
    fixed["questions"] = questions
    return fixed


def validation_problems(exc: BaseException, limit: int = 10) -> list[str]:
    """検査に落ちた場所と種類だけ（例: questions.0.header:string_too_long）。値は含めない。"""
    if not isinstance(exc, ValidationError):
        return []
    return [".".join(str(part) for part in error["loc"]) + ":" + error["type"]
            for error in exc.errors()[:limit]]


def gemini_response_schema(model: type[BaseModel]) -> dict:
    """Keep the wire schema small; enforce all application constraints after parsing.

    Nested bounded arrays in the full interview schema exceed Vertex's structured
    output complexity budget. Keep shape, required fields and enums on the wire.
    """
    def simplify(schema):
        result = {key: value for key, value in schema.items()
                  if key in {"type", "$ref", "required", "enum", "description"}}
        # google-genai の Schema は enum を文字列しか受け付けない（数値の選択肢
        # 例: production_day_hours の 8/12/16/24 で、送る前に ValidationError になる）。
        # 文字列以外の列挙は外し、値の検査は受け取った後の Pydantic に任せる。
        if "enum" in result and not all(isinstance(value, str) for value in result["enum"]):
            del result["enum"]
        for key in ("properties", "$defs"):
            if key in schema:
                result[key] = {name: simplify(value) for name, value in schema[key].items()}
        if "items" in schema:
            result["items"] = simplify(schema["items"])
        if "anyOf" in schema:
            result["anyOf"] = [simplify(value) for value in schema["anyOf"]]
        return result

    return simplify(model.model_json_schema())


# 失敗の理由。「もう一度開始するか省略してください」だけでは、何度やっても同じ結果に
# なるのか、待てば直るのかが分からない。分かる範囲で見分けて書き分ける。
INTERVIEW_ERRORS = {
    "timeout": "ヒアリングが時間内に終わりませんでした。もう一度開始するか、ヒアリングを省略してください。",
    "transport": "AIとの接続が切れました。少し待ってから、もう一度開始してください。",
    "format": "AIの回答を要件として読み取れませんでした。もう一度開始するか、ヒアリングを省略してください。",
    # 枠切れは、やり直しても直らない。待つか、別の生成元へ移るしかない。
    "quota": "AIの利用枠の上限に達しました。生成元でGeminiを選ぶか、ヒアリングを省略して進めてください。",
    "provider_busy": "Gemini APIが混み合っています。少し待ってからもう一度開始するか、ヒアリングを省略してください。",
    "failed": "要件を整理できませんでした。もう一度開始するか、ヒアリングを省略してください。",
}


def interview_failure_code(exc: BaseException) -> str:
    """例外を、利用者へ返せる分類へ落とす。例外の文面そのものは決して返さない。

    実行環境の例外文には接続先や生成物が入りうる。外へ出すのは分類だけにする。
    実行基盤側の例外（CodexTransportClosed / QuotaExceeded）は、ここから import すると
    domain → core/worker の逆向き参照になる。名前で見分ける。
    """
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return "timeout"
    name = type(exc).__name__
    if name == "CodexTransportClosed":
        return "transport"
    if name in {"QuotaExceeded", "CodexUsageLimited"}:
        return "quota"
    if name == "ProviderUnavailable":
        return "provider_busy"
    if isinstance(exc, (ValidationError, ValueError)):
        return "format"
    # google-genai は枠切れも要求の誤りも同じ ClientError で投げる。型名だけでは
    # 見分けられないので、状態コードで分ける。429 をやり直しの効かない「失敗」に
    # 落とすと、待てば通るものを「もう一度」と案内してしまう。
    if failure_status(exc) == 429:
        return "quota"
    if failure_status(exc) == 503:
        return "provider_busy"
    return "failed"


def redacted_reason(exc: BaseException) -> str:
    """理由を短く1行にする。生成物や接続先をそのまま記録へ流さない。

    例外文には仕様案（利用者の入力）や実行環境の事情が入りうる。運用の記録として
    要るのは「何が起きたか」なので、既知の合図だけを拾って、それ以外は型名に留める。
    """
    text = f"{type(exc).__name__} {exc}"
    for signal in ("RESOURCE_EXHAUSTED", "INVALID_ARGUMENT", "PERMISSION_DENIED",
                   "NOT_FOUND", "UNAUTHENTICATED", "DEADLINE_EXCEEDED", "UNAVAILABLE",
                   "response_schema", "token", "quota"):
        if signal in text:
            return signal
    return type(exc).__name__


def failure_status(exc: BaseException) -> int | None:
    """HTTPの状態コードを取り出す。無ければ None。

    実行環境の例外文は外へ出さない。ここで取るのは数字だけで、運用の記録と
    分類にしか使わない。
    """
    for name in ("code", "status_code"):
        value = getattr(exc, name, None)
        if isinstance(value, int) and 100 <= value < 600:
            return value
    return None


# Codexは「JSONだけを返す」と指示しても、前置きやコードフェンスを付けてくることがある。
# 厳密に読んで失敗させると、中身は正しいのにヒアリングごとやり直しになる。
FENCE = re.compile(r"```(?:json)?\s*(?P<body>.+?)\s*```", re.S)


def interview_json(text: str) -> str:
    """モデルの返事からProjectInputのJSONを取り出す。

    受け取った形をここで整えるだけで、中身の検証はProjectInput側に任せる。
    見つからなければ元の文字列を返し、検証側で落とす。
    """
    candidate = (text or "").strip()
    if match := FENCE.search(candidate):
        candidate = match["body"].strip()
    if candidate.startswith("{") and candidate.endswith("}"):
        return candidate
    # 前置きが付いた場合。最も外側の { から } までを取る。
    start, end = candidate.find("{"), candidate.rfind("}")
    return candidate[start:end + 1] if 0 <= start < end else candidate
