"""作成前の選択内容から、利用目的の編集用下書きを組み立てる。"""
import json

from pydantic import BaseModel, ConfigDict, Field

from backend.domain.projects import CreationProfile, TableSpec


class PurposeDraftInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=80)
    creation_profile: CreationProfile
    tables: list[TableSpec] = Field(min_length=1, max_length=12)


class PurposeDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    purpose: str = Field(min_length=5, max_length=500)


GOAL_LABELS = {
    "production_performance": "実績推移", "production_progress": "累計生産数の進捗",
    "yield": "歩留の推移", "production_by_time": "時間帯別の生産性",
    "pareto": "パレート図", "defect_pareto": "パレート図",
    "quality_control_chart": "管理図", "gantt": "ガントチャート",
    "equipment_gantt": "ガントチャート", "plan_actual_gantt": "ガントチャート",
    "daily_records": "日報・点検・帳票", "ledger": "台帳・一覧管理",
    "schedule": "予定・進捗管理", "inquiry": "問い合わせ・タスク管理",
    "file_visualization": "グラフ化", "reporting": "集計・レポート",
    "records": "データ管理", "ai_processing": "AI処理（Geminiで読み取り・分析）",
}


PURPOSE_DRAFT_INSTRUCTION = """あなたは、業務アプリの利用目的を書く編集者です。
渡された情報だけを使い、利用者が修正するための元原稿を日本語で1文作ってください。
誰が、何を行い、どのような業務上の状態を目指すかが分かる、40〜160文字程度の文にします。
機能の羅列、技術名、AIへの言及、広告・スローガン調の表現は避けてください。
入力に無い制度、効果、担当組織、数値を作らないでください。"""


def purpose_context(value: PurposeDraftInput) -> str:
    """モデルへ渡す情報を絞る。CSVのサンプル値や元ファイルは含めない。"""
    profile = value.creation_profile
    mapped = {mapping.column: mapping.role for mapping in profile.column_mappings}
    document = {
        "app_name": value.name,
        "app_pattern": profile.app_pattern,
        "work_category": profile.work_category,
        "goals": [GOAL_LABELS.get(goal, goal) for goal in profile.goals],
        "other_goal": profile.other_goal or None,
        "production_day_start": profile.production_day_start,
        "production_day_hours": profile.production_day_hours,
        "default_period": profile.default_period,
        "tables": [{
            "name": table.name,
            "kind": table.kind,
            "fields": [{"name": field.name, "kind": field.kind,
                        "role": mapped.get(field.name, "none")}
                       for field in table.fields],
        } for table in value.tables],
    }
    return json.dumps(document, ensure_ascii=False, separators=(",", ":"))
