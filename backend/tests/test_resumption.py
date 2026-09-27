"""中断した依頼のやり直し。書けていたところを捨てさせない。"""
import asyncio
from types import SimpleNamespace
from uuid import uuid4
from backend.domain.generation import MAX_LISTED_IN_PROMPT, resumption_note, scaffold_files
from backend.domain.projects import ProjectInput
from backend.tests.test_projects import INPUT

SPEC = ProjectInput(**INPUT)


def test_the_note_names_the_files_and_forbids_starting_over():
    note = resumption_note(["backend/main.py", "frontend/index.html"])
    assert "Do not start over" in note
    assert "- backend/main.py" in note and "- frontend/index.html" in note


def test_a_long_list_is_trimmed_so_the_request_stays_readable():
    note = resumption_note([f"backend/m{i}.py" for i in range(MAX_LISTED_IN_PROMPT + 25)])
    listed = [line for line in note.splitlines() if line.startswith("- backend/")]
    assert len(listed) == MAX_LISTED_IN_PROMPT
    assert "ほか 25 件" in note


# ---- agent の組み立て -------------------------------------------------------

class Recorder:
    def __init__(self):
        self.prompts, self.messages = [], []

    def record(self, kind, message, **kwargs):
        self.messages.append(message)


def build_agent(tmp_path):
    from backend.worker.agent import Agent, AgentSettings
    settings = AgentSettings(_env_file=None, user_id=uuid4(), token="t" * 40,
                             root=tmp_path, generator="gemini",
                             gemini_model="gemini-3.8-flash")
    return Agent(settings)


def run_generate(agent, tmp_path, instruction=None):
    """geminiのターンを差し替えて、実際に渡る依頼文だけを取り出す。"""
    captured = {}

    async def fake_turn(workspace, model, specification, given, progress, **kwargs):
        captured["notes"] = kwargs.get("notes", "")
        captured["instruction"] = given
        progress.record("codex", "done", response=True)

    import backend.worker.agent as module
    original = module.gemini_turn
    module.gemini_turn = fake_turn
    try:
        payload = SimpleNamespace(job_id=uuid4(), project_id=uuid4(), specification=SPEC,
                                  instruction=instruction, model=None, effort=None,
                                  generator="gemini")
        asyncio.run(agent.generate(payload))
        captured["workspace"] = agent.workspace_path(payload.project_id)
        return captured
    finally:
        module.gemini_turn = original


def test_a_fresh_project_is_not_treated_as_a_resumption(tmp_path):
    """共通部品を置いただけの作業場所は「前回の続き」ではない。"""
    agent = build_agent(tmp_path)
    captured = run_generate(agent, tmp_path)
    assert "earlier attempt" not in captured["notes"]


def test_work_left_by_a_failed_attempt_is_carried_into_the_next_request(tmp_path):
    agent = build_agent(tmp_path)
    first = run_generate(agent, tmp_path)
    workspace = first["workspace"]
    # 前回が途中で落ち、いくつか書けた状態を作る。
    (workspace / "backend").mkdir(parents=True, exist_ok=True)
    (workspace / "backend" / "main.py").write_text("app = 1\n", encoding="utf-8")

    from types import SimpleNamespace as NS
    captured = {}

    async def fake_turn(ws, model, specification, given, progress, **kwargs):
        captured["notes"] = kwargs.get("notes", "")
        progress.record("codex", "done", response=True)

    import backend.worker.agent as module
    original = module.gemini_turn
    module.gemini_turn = fake_turn
    try:
        # 同じプロジェクトで、もう一度「承認した仕様から作成」を押す。
        project_id = workspace.parent.name
        asyncio.run(agent.generate(NS(job_id=uuid4(), project_id=project_id, specification=SPEC,
                                      instruction=None, model=None, effort=None, generator="gemini")))
    finally:
        module.gemini_turn = original
    assert "earlier attempt" in captured["notes"]
    assert "- backend/main.py" in captured["notes"]
    # 共通部品は「前回書けたもの」ではないので並べない。
    assert "- frontend/src/styles/tokens.css" not in captured["notes"]


def test_a_change_request_does_not_get_the_resumption_note(tmp_path):
    """変更依頼は元から既存コードを直す文面。二重に言わない。"""
    agent = build_agent(tmp_path)
    first = run_generate(agent, tmp_path)
    workspace = first["workspace"]
    (workspace / "backend").mkdir(parents=True, exist_ok=True)
    (workspace / "backend" / "main.py").write_text("app = 1\n", encoding="utf-8")

    captured = {}

    async def fake_turn(ws, model, specification, given, progress, **kwargs):
        captured["notes"] = kwargs.get("notes", "")
        progress.record("codex", "done", response=True)

    import backend.worker.agent as module
    from types import SimpleNamespace as NS
    original = module.gemini_turn
    module.gemini_turn = fake_turn
    try:
        asyncio.run(agent.generate(NS(job_id=uuid4(), project_id=workspace.parent.name,
                                      specification=SPEC, instruction="ボタンを足して",
                                      model=None, effort=None, generator="gemini")))
    finally:
        module.gemini_turn = original
    assert "earlier attempt" not in captured["notes"]


def test_the_scaffold_is_not_written_again_over_existing_work(tmp_path):
    """2回目に共通部品を上書きすると、モデルの修正を巻き戻す。"""
    agent = build_agent(tmp_path)
    first = run_generate(agent, tmp_path)
    workspace = first["workspace"]
    target = workspace / next(iter(scaffold_files()))
    target.write_text("/* モデルが直した */\n", encoding="utf-8")

    async def fake_turn(ws, model, specification, given, progress, **kwargs):
        progress.record("codex", "done", response=True)

    import backend.worker.agent as module
    from types import SimpleNamespace as NS
    original = module.gemini_turn
    module.gemini_turn = fake_turn
    try:
        asyncio.run(agent.generate(NS(job_id=uuid4(), project_id=workspace.parent.name,
                                      specification=SPEC, instruction=None,
                                      model=None, effort=None, generator="gemini")))
    finally:
        module.gemini_turn = original
    assert target.read_text() == "/* モデルが直した */\n"
