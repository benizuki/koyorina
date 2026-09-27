from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class FieldSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=50)
    kind: Literal["text", "longtext", "number", "date", "bool"] = "text"
    required: bool = True


class TableSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=60)
    kind: Literal["record", "master"] = "record"
    fields: list[FieldSpec] = Field(min_length=1, max_length=20)

    @field_validator("fields")
    @classmethod
    def unique_fields(cls, fields):
        names = [f.name.casefold() for f in fields]
        if len(names) != len(set(names)):
            raise ValueError("同じテーブルに重複した項目名があります。")
        return fields


ColumnRole = Literal[
    "none", "timestamp", "equipment", "product", "lot", "quantity", "result",
    "good_count", "defect_count", "defect_category", "status", "start_time", "end_time",
    "target", "quality_value", "quality_item", "unit", "specification_upper",
    "specification_lower", "planned_start", "planned_end", "actual_start", "actual_end",
    "category", "value",
    # 事務・バックオフィスの台帳や伝票でよく出る列。
    "location", "department", "person", "partner", "document_number", "amount", "unit_price",
    "due_date", "note",
]


class SampleColumn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=100)
    kind: Literal["text", "number", "date", "datetime", "bool"] = "text"
    samples: list[str] = Field(default_factory=list, max_length=5)
    suggested_role: ColumnRole = "none"


class SampleDataProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    filename: str = Field(min_length=1, max_length=120)
    sheet: str | None = Field(default=None, max_length=100)
    row_count: int = Field(ge=0, le=1_000_000_000)
    columns: list[SampleColumn] = Field(min_length=1, max_length=100)
    warnings: list[str] = Field(default_factory=list, max_length=10)


class ColumnMapping(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    column: str = Field(min_length=1, max_length=100)
    role: ColumnRole


class CreationProfile(BaseModel):
    """最初の選択とサンプルデータの読み取り結果。元ファイルは保存しない。"""
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    app_pattern: Literal["local_file_visualization", "data_management", "file_import"]
    # prompt: 帳票やサンプルを選ばず、利用者の依頼文だけを仕様にして作る。
    # 従来の手順で作った仕様のJSONを変えないよう、guidedは出力しない。
    mode: Literal["guided", "prompt"] = Field(default="guided", exclude_if=lambda value: value == "guided")
    work_category: Literal["manufacturing", "indirect", "other"] = "indirect"
    app_type: Literal["records", "visualization", "both"] = "records"
    goals: list[str] = Field(default_factory=list, max_length=12)
    other_goal: str = Field(default="", max_length=500)
    production_day_start: str | None = Field(default=None, pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    # 1日の稼働時間（時間）。24は1日通し、8は1直など。開始時刻からこの長さを時間帯別の横軸にする。
    production_day_hours: Literal[8, 12, 16, 24] | None = None
    # 画面を開いたときの期間。「直近」は今日ではなく、読み込んだデータの最新の日から数える。
    default_period: Literal["latest_day", "last_3_days", "last_7_days", "last_30_days", "all"] | None = None
    sample_data: SampleDataProfile | None = None
    column_mappings: list[ColumnMapping] = Field(default_factory=list, max_length=100)

    @model_validator(mode="before")
    @classmethod
    def compatible_pattern(cls, value):
        """旧データから3パターンを補い、構成と仕事分類を最初の選択へ揃える。"""
        if not isinstance(value, dict):
            return value
        result = dict(value)
        if not result.get("app_pattern"):
            result["app_pattern"] = {
                "visualization": "local_file_visualization",
                "both": "file_import",
            }.get(result.get("app_type"), "data_management")
        result["app_type"] = {
            "local_file_visualization": "visualization",
            "data_management": "records",
            "file_import": "both",
        }[result["app_pattern"]]
        result["work_category"] = {
            "local_file_visualization": "manufacturing",
            "data_management": "indirect",
            "file_import": "other",
        }[result["app_pattern"]]
        return result

    @field_validator("goals")
    @classmethod
    def valid_goals(cls, goals):
        cleaned = [goal.strip() for goal in goals]
        if any(not goal or len(goal) > 60 for goal in cleaned):
            raise ValueError("作りたい内容を確認してください。")
        if len({goal.casefold() for goal in cleaned}) != len(cleaned):
            raise ValueError("作りたい内容が重複しています。")
        return cleaned

    @field_validator("column_mappings")
    @classmethod
    def unique_column_mappings(cls, mappings):
        names = [mapping.column.casefold() for mapping in mappings]
        if len(names) != len(set(names)):
            raise ValueError("同じ列の割り当てが重複しています。")
        return mappings

    @field_validator("sample_data")
    @classmethod
    def discard_sample_values(cls, sample_data):
        """列構成は残すが、画面確認に使った実データはプロジェクトへ保存しない。"""
        if sample_data is None:
            return None
        return sample_data.model_copy(update={
            "columns": [column.model_copy(update={"samples": []}) for column in sample_data.columns],
        })


class LlmAvailability(BaseModel):
    """テナントで生成アプリ用のGeminiが用意されているか。生成AIが「使ってよい」と分かるための印。

    接続先や秘密は入れない。アプリは実行時に環境変数から読む（AGENTS.md）。
    仕様として保存はせず、生成を依頼するときにテナントの設定から付ける。
    """
    model_config = ConfigDict(extra="forbid")
    available: bool = True
    backend: Literal["gemini_api", "vertex"]


class ProjectInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=80)
    purpose: str = Field(min_length=5, max_length=2000)
    # 社内の招待制が前提のため、利用者は聞かずにチーム共有で固定する。
    # 既存データと生成側の契約を変えないよう、値そのものは残す。
    audience: Literal["self", "team"] = "team"
    # プロンプトから作る場合だけ空を許す。従来の手順では項目を1つ以上求める。
    fields: list[FieldSpec] = Field(default_factory=list, max_length=20)
    tables: list[TableSpec] = Field(default_factory=list, max_length=12)
    # ヒアリングで確定した、テーブル定義以外の業務要件。
    requirements: list[str] = Field(default_factory=list, max_length=40)
    # 空なら仕様から利用者向けの依頼文を自動生成する。保存後はこの文章を生成AIへ渡す。
    generation_prompt: str = Field(default="", max_length=10000,
                                   exclude_if=lambda value: not value)
    # 旧仕様のJSONを変えず、新しい作成フローを使ったときだけ生成入力へ加える。
    creation_profile: CreationProfile | None = Field(default=None, exclude_if=lambda value: value is None)
    # 生成の依頼時にだけ付ける（保存しない）。画面から送られても無視する。
    llm: LlmAvailability | None = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode="before")
    @classmethod
    def purpose_from_prompt(cls, value):
        """プロンプトから作るときは、依頼文の書き出しを一覧に出す目的として使う。

        目的を別に書かせると依頼文と食い違う。依頼文を直せば目的も追従させる。
        """
        if not isinstance(value, dict) or not _prompt_mode(value.get("creation_profile")):
            return value
        prompt = str(value.get("generation_prompt") or "").strip()
        if len(prompt) < 5:
            raise ValueError("AIへの依頼文を5文字以上で入力してください。")
        lines = [line.strip() for line in prompt.splitlines() if line.strip()]
        purpose = lines[0] if len(lines[0]) >= 5 else " ".join(lines)
        return {**value, "purpose": purpose[:200]}

    @model_validator(mode="after")
    def fields_unless_prompt(self):
        if not self.fields and not (self.creation_profile and self.creation_profile.mode == "prompt"):
            raise ValueError("入力項目を1つ以上登録してください。")
        return self

    @field_validator("fields")
    @classmethod
    def unique_fields(cls, fields):
        names = [f.name.casefold() for f in fields]
        if len(names) != len(set(names)):
            raise ValueError("項目名が重複しています。")
        return fields

    @field_validator("tables")
    @classmethod
    def unique_tables(cls, tables):
        names = [table.name.casefold() for table in tables]
        if len(names) != len(set(names)):
            raise ValueError("テーブル名が重複しています。")
        if tables and not any(table.kind == "record" for table in tables):
            raise ValueError("帳票・業務テーブルを1つ以上登録してください。")
        if sum(len(table.fields) for table in tables) > 100:
            raise ValueError("全テーブルの項目数は100件までです。")
        return tables

    @field_validator("requirements")
    @classmethod
    def valid_requirements(cls, requirements):
        cleaned = [item.strip() for item in requirements]
        if any(not item or len(item) > 500 for item in cleaned):
            raise ValueError("要件は1件500文字以内で入力してください。")
        if len({item.casefold() for item in cleaned}) != len(cleaned):
            raise ValueError("同じ要件が重複しています。")
        return cleaned


def _prompt_mode(profile) -> bool:
    if isinstance(profile, CreationProfile):
        return profile.mode == "prompt"
    return isinstance(profile, dict) and profile.get("mode") == "prompt"


def project_tables(project) -> list[dict]:
    tables = project.tables or []
    if tables:
        return tables
    if not project.fields:
        return []
    return [{"name": project.name, "kind": "record", "fields": project.fields}]


def approve(current_revision: int, requested_revision: int, status: str):
    if current_revision != requested_revision:
        raise ValueError("仕様が更新されています。再読み込みして確認してください。")
    if status not in {"draft", "approved"}:
        raise ValueError("この状態では仕様を承認できません。")


def specification(project) -> dict:
    profile = project.creation_profile
    app_pattern = (profile.get("app_pattern") if isinstance(profile, dict)
                   else profile.app_pattern if profile else "data_management")
    app_type = (profile.get("app_type") if isinstance(profile, dict)
                else profile.app_type if profile else "records")
    if not app_pattern:
        app_pattern = {"visualization": "local_file_visualization",
                       "both": "file_import"}.get(app_type, "data_management")
    screens = (["データ取込・列の確認", "ダッシュボード・グラフ"]
               if app_type == "visualization" else
               ["一覧・検索", "登録・編集", "データ取込・ダッシュボード"]
               if app_type == "both" else ["一覧・検索", "登録・編集"])
    return {
        "name": project.name, "purpose": project.purpose,
        "audience": project.audience, "fields": project.fields,
        "tables": project_tables(project),
        "requirements": project.requirements or [],
        "generation_prompt": project.generation_prompt or "",
        "creation_profile": project.creation_profile,
        # ログインはKoyorinaの前段で完了しているため、生成アプリの画面要件にしない。
        "screens": screens,
        "authentication": (
            "Koyorinaでログイン済みの利用者情報を、プロジェクト固有の署名付き"
            "X-Forge-*ヘッダーで受け取る。生成アプリ内にログイン画面を作らない"
        ),
        "stack": ("Vue 3 + Vuetify。FastAPIは生成済みファイルを配信するWebサーバーとしてのみ使用し、"
                  "業務API・SQLAlchemy・データベースは使用しない"
                  if app_pattern == "local_file_visualization" else
                  "Vue 3 + Vuetify + FastAPI + SQLAlchemy"),
        "preview": "認証付きの確認環境へ自動反映",
        "production": "確認後に所有者が承認して反映",
        "storage": ("利用者が選んだファイルをブラウザ内だけで処理し、サーバーへ送信・保存しない"
                    if app_pattern == "local_file_visualization" else
                    "ローカルはSQLite（設定不要）／公開環境はPostgreSQL（永続ディスク・外部バックアップ）"),
    }
