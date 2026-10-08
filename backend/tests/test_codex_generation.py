import asyncio
import io
import json
import stat
import zipfile
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from backend.core.codex_bridge import CodexBridge, isolated_environment
from pydantic import ValidationError
from backend.domain.generation import (artifact_path_is_allowed, code_bundle_from_workspace,
                                       code_bundle_from_zip, conventions, CodeBundle, SourceFile,
                                       generation_permission_args, generation_prompt, model_settings,
                                       scaffold_files, validation_failure_code)
from backend.domain.interview import INTERVIEW_ERRORS
from backend.domain.projects import ProjectInput, specification
from backend.worker.agent import Agent, AgentSettings, GenerationInput, create_agent
from backend.worker.controller import ControllerSettings, resources
from backend.domain.preview import dependency_digest
from backend.tests.test_projects import INPUT


PYPROJECT = ('[project]\nname = "ledger"\nversion = "0.1.0"\nrequires-python = ">=3.14"\ndependencies = ["fastapi>=0.141", "uvicorn[standard]>=0.34"]\n')

BUNDLE = {"files": [{"path": "pyproject.toml", "content": PYPROJECT},
                    {"path": "backend/main.py", "content": "from fastapi import FastAPI\napp = FastAPI()\n@app.get('/')\ndef index(): return {'status': 'ok'}\n"},
                    {"path": "frontend/package.json", "content": '{"scripts":{"build":"vite build"}}'},
                    {"path": "frontend/src/App.vue", "content": "<template><p>台帳</p></template>"}]}


class FakeBridge:
    calls = []
    account_type = "chatgpt"
    turn_status = "completed"
    final_text = "アプリのソースを作成しました。"

    def __init__(self, *args, **kwargs):
        self.notifications = asyncio.Queue()
        self.process = SimpleNamespace(returncode=None)
        self.calls = []
        self.login_done = asyncio.Event()
        self.workspace = None
        self.turns = 0
        self.allow_user_input = kwargs.get("allow_user_input", False)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.process.returncode = 0

    async def call(self, method, params):
        self.calls.append((method, params))
        if method == "account/read":
            return {"account": {"type": self.account_type, "email": "person@example.test", "planType": "plus"}}
        if method == "account/login/start":
            return {"type": "chatgptDeviceCode", "loginId": str(uuid4()), "verificationUrl": "https://auth.openai.com/codex/device", "userCode": "TEST-ONLY"}
        if method == "thread/start":
            if params.get("cwd"):
                self.workspace = Path(params["cwd"])
            return {"thread": {"id": "thread1"}}
        if method == "collaborationMode/list":
            return {"data": [{"mode": "plan", "name": "Plan", "model": None,
                              "reasoning_effort": "medium"}]}
        if method == "model/list":
            return {"data": [{"id": "gpt-6-astra", "isDefault": True}]}
        if method == "turn/start":
            # cwd は turn/start にも乗る。ここで拾わないと、スレッドを使い回す
            # 経路（2回目以降の生成）で作業場所を知らないまま進んでしまう。
            if params.get("cwd"):
                self.workspace = Path(params["cwd"])
            if self.allow_user_input:
                result = ProjectInput(**INPUT).model_dump_json()
                await self.notifications.put({"method": "item/completed", "params": {
                    "threadId": "thread1", "turnId": "turn1", "item": {
                        "type": "agentMessage", "phase": "final_answer", "text": result}}})
                await self.notifications.put({"method": "turn/completed", "params": {
                    "threadId": "thread1", "turn": {"id": "turn1", "status": "completed"}}})
                return {"turn": {"id": "turn1"}}
            for source in BUNDLE["files"]:
                target = self.workspace / source["path"]
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(source["content"])
            await self.notifications.put({"method": "item/completed", "params": {"threadId": "thread1", "turnId": "turn1", "item": {"type": "agentMessage", "phase": "final_answer", "text": self.final_text}}})
            # 消費は「そのスレッドの累計」で通知される。ターンが進むほど増える。
            # 同じ値を返し続ける形にしておくと、差分の取り違えを検出できない。
            self.turns += 1
            n = self.turns
            await self.notifications.put({"method": "thread/tokenUsage/updated", "params": {
                "threadId": "thread1", "turnId": "turn1", "tokenUsage": {
                    "total": {"inputTokens": 90 * n, "outputTokens": 10 * n,
                              "cachedInputTokens": 40 * n, "totalTokens": 100 * n},
                    "last": {"inputTokens": 90, "outputTokens": 10, "cachedInputTokens": 40,
                             "totalTokens": 100}}}})
            await self.notifications.put({"method": "turn/completed", "params": {"threadId": "thread1", "turn": {"id": "turn1", "status": self.turn_status}}})
            return {"turn": {"id": "turn1"}}
        return {}

    async def wait_for_login(self, login_id):
        await self.login_done.wait()
        return True


PROJECT_ID = UUID("6459c3f2-3731-4949-9ff8-b82ef29775d6")


@pytest.fixture(autouse=True)
def toolchain_is_not_the_subject(monkeypatch):
    """依存の導入と検査は既定で素通しにする。

    実機の uv / npm の有無で、関係のない試験が落ちたり通ったりしないようにする。
    導入と検査そのものは test_toolchain.py と、明示的に差し替える試験で見る。
    """
    from backend.worker import toolchain_runner

    async def nothing(*args, **kwargs):
        return []

    async def available(*args, **kwargs):
        return ""

    monkeypatch.setattr(toolchain_runner, "install", nothing)
    monkeypatch.setattr(toolchain_runner, "verify", nothing)
    # 手元には codex が無い。探査を素通しにしないと、全部「検査できない」になる。
    monkeypatch.setattr(toolchain_runner, "sandbox_problem", available)


def settings(tmp_path):
    return AgentSettings(_env_file=None, user_id=uuid4(), token="test-only-" + "x" * 40, root=tmp_path)


@pytest.mark.parametrize("path", ["../auth.json", "/etc/passwd", "backend/../../secret.py", ".env", "backend/.env.json", "backend//main.py", "backend\\main.py", "Dockerfile", "frontend/x.svg"])
def test_artifact_paths_rejected(path):
    with pytest.raises(ValueError):
        SourceFile(path=path, content="{}")


@pytest.mark.parametrize("path", [".env.example", ".gitignore", "backend/alembic.ini",
    "backend/migrations/001_create.sql", "config/example.yaml"])
def test_common_safe_source_paths_allowed(path):
    assert SourceFile(path=path, content="EXAMPLE=value\n")


def test_validation_failure_is_classified_without_source_content():
    secret_source = "TOP_SECRET_GENERATED_SOURCE"
    with pytest.raises(ValidationError) as error:
        CodeBundle.model_validate({"files": [{"path": "frontend/unsafe.svg", "content": secret_source}]})
    assert validation_failure_code(error.value) == "validation_files"
    assert secret_source not in validation_failure_code(error.value)


NO_ENTRY = {"files": [
    {"path": "pyproject.toml", "content": PYPROJECT},
    {"path": "backend/main.py",
     "content": "from fastapi import FastAPI\napp = FastAPI()\n@app.get('/items')\ndef items(): return []\n"},
    {"path": "frontend/package.json", "content": '{"scripts":{"build":"vite build"}}'},
    {"path": "frontend/src/App.vue", "content": "<template><p>台帳</p></template>"}]}


def test_a_missing_screen_entry_is_not_called_a_syntax_error():
    """起動条件と構文エラーは直し方が違う。同じ value_error で来るので見分ける。"""
    from backend.domain.generation import validation_problems
    with pytest.raises(ValidationError) as error:
        CodeBundle.model_validate(NO_ENTRY)
    assert validation_failure_code(error.value) == "validation_contract"
    problems = validation_problems(error.value)
    assert problems and "画面の入口" in problems[0]
    # 説明文だけを返す。pydanticの既定には生成コード全文が入っている。
    assert all("FastAPI" not in problem for problem in problems)


def test_starting_a_preview_says_why_the_code_was_refused():
    """「開始」だけ失敗して「再起動」は動く、という状態の理由を利用者へ返す。

    以前はValidationErrorが素通りして500になり、「管理者に接続設定を確認して」と
    表示されていた。接続は何ともなっていないので、そこから原因には辿り着けない。
    """
    from backend.api.generation import job_bundle
    job = SimpleNamespace(status="generated", source_type="local_codex", artifact=NO_ENTRY,
                          id=str(uuid4()))
    with pytest.raises(HTTPException) as error:
        asyncio.run(job_bundle(SimpleNamespace(local_codex_enabled=True), None, job))
    assert error.value.status_code == 409
    assert "検査を通りませんでした" in error.value.detail
    assert "画面の入口" in error.value.detail
    assert "FastAPI" not in error.value.detail


def test_bundle_and_provider_policy():
    assert CodeBundle.model_validate(BUNDLE)
    with pytest.raises(ValueError):
        CodeBundle.model_validate({"files": BUNDLE["files"] * 2})
    prompt = generation_prompt(ProjectInput(**INPUT))
    # 規約はAGENTS.mdへ移した。プロンプトは作業指示と仕様だけを運ぶ。
    assert "AGENTS.md" in prompt and INPUT["name"] in prompt
    rules = conventions()
    # 接続先はテナントの設定から環境変数で届く。コードに書かせない。
    assert "`genai.Client()` を**引数なしで**作る" in rules and "`GEMINI_MODEL`" in rules
    assert "アプリのコード生成に Gemini を使うことは決してしない" in rules
    assert "ローカル実行のために `DATABASE_URL` は任意" in rules
    assert "SQLite と PostgreSQL の両方で動く形に保つ" in rules
    assert "APP_FORWARD_SECRET" in rules and "import.meta.env.BASE_URL" in rules
    assert "集計カードと主グラフ1個" in rules
    assert "広告・スローガン調" in rules and "アプリケーションバーへ置く" in rules


def test_workspace_bundle_ignores_cache_and_rejects_symlinks(tmp_path):
    for source in BUNDLE["files"]:
        target = tmp_path / source["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source["content"])
    cache = tmp_path / "backend/__pycache__"
    cache.mkdir()
    (cache / "main.pyc").write_bytes(b"binary")
    assert CodeBundle.model_validate(code_bundle_from_workspace(tmp_path).model_dump())
    (tmp_path / "backend/link.py").symlink_to(tmp_path / "backend/main.py")
    with pytest.raises(ValueError, match="シンボリックリンク"):
        code_bundle_from_workspace(tmp_path)


def test_workspace_bundle_rejects_oversized_source_before_validation(tmp_path):
    (tmp_path / "backend").mkdir()
    (tmp_path / "backend/main.py").write_text("x" * 200_001)
    with pytest.raises(ValueError, match="大きすぎる"):
        code_bundle_from_workspace(tmp_path)


def zipped(entries, *, root="app"):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path, content in entries.items():
            archive.writestr(f"{root}/{path}" if root else path, content)
    return buffer.getvalue()


def test_local_zip_bundle_strips_root_and_ignores_local_outputs():
    entries = {item["path"]: item["content"] for item in BUNDLE["files"]}
    entries.update({".env": "SECRET=never-store", "package-lock.json": "{}",
                    "node_modules/ignored.js": "ignored", ".git/config": "ignored"})
    bundle = code_bundle_from_zip(zipped(entries))
    paths = {item.path for item in bundle.files}
    assert paths == {item["path"] for item in BUNDLE["files"]}
    assert "never-store" not in bundle.model_dump_json()


@pytest.mark.parametrize("path", ["../outside.py", "/absolute.py", "backend\\main.py"])
def test_local_zip_rejects_unsafe_paths(path):
    with pytest.raises(ValueError, match="安全でない"):
        code_bundle_from_zip(zipped({path: "pass\n"}, root=""))


def test_local_zip_rejects_symlink():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        info = zipfile.ZipInfo("backend/main.py")
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(info, "target.py")
    with pytest.raises(ValueError, match="シンボリックリンク"):
        code_bundle_from_zip(buffer.getvalue())


def test_only_authenticated_chatgpt_can_generate(tmp_path):
    async def run():
        agent = Agent(settings(tmp_path), FakeBridge)
        bridge = await agent.connect()
        bridge.account_type = "apiKey"
        with pytest.raises(HTTPException) as error:
            await agent.start_generation(GenerationInput(job_id=uuid4(), project_id=PROJECT_ID, specification=INPUT))
        assert error.value.status_code == 409
        assert not any(method in {"thread/start", "turn/start"} for method, _ in bridge.calls)
        await agent.close()
    asyncio.run(run())


def test_interview_question_is_compacted_and_answered():
    message = {"id": "question-1", "method": "item/tool/requestUserInput", "params": {
        "threadId": "thread1", "turnId": "turn1", "questions": [{
            "id": "sharing", "header": "共有範囲", "question": "誰が編集しますか？",
            "options": [{"label": "担当者", "description": "担当者だけが編集します。"},
                        {"label": "チーム", "description": "チーム全員が編集します。"}],
            "isOther": False}]}}
    compact = CodexBridge._compact_user_input(message)
    assert compact["params"]["requestId"] == "question-1"
    assert compact["params"]["questions"][0] == {
        "id": "sharing", "header": "共有範囲", "question": "誰が編集しますか？",
        "options": [{"label": "担当者", "description": "担当者だけが編集します。"},
                    {"label": "チーム", "description": "チーム全員が編集します。"}],
        "is_other": False}

    async def answer():
        bridge = CodexBridge("codex", Path("/tmp"), str(uuid4()), allow_user_input=True)
        sent = []
        async def capture(payload):
            sent.append(payload)
        bridge.send = capture
        await bridge.answer_user_input("question-1", {"sharing": ["チーム"]})
        assert sent == [{"id": "question-1", "result": {"answers": {
            "sharing": {"answers": ["チーム"]}}}}]
    asyncio.run(answer())


def test_multiple_tables_are_in_the_generation_specification():
    project = ProjectInput(**INPUT, tables=[
        {"name": "貸出台帳", "kind": "record", "fields": INPUT["fields"]},
        {"name": "備品マスター", "kind": "master", "fields": [
            {"name": "備品番号", "kind": "text", "required": True}]}])
    prompt = generation_prompt(project)
    assert "貸出台帳" in prompt and "備品マスター" in prompt


def test_user_visible_generation_prompt_excludes_internal_instructions():
    from backend.domain.generation import user_generation_prompt

    project = ProjectInput(**INPUT, tables=[
        {"name": "貸出台帳", "kind": "record", "fields": INPUT["fields"]},
        {"name": "備品マスター", "kind": "master", "fields": [
            {"name": "備品番号", "kind": "text", "required": True}]}])
    visible = user_generation_prompt(project)

    assert INPUT["name"] in visible and INPUT["purpose"] in visible
    assert "貸出台帳" in visible and "備品マスター" in visible
    assert "AGENTS.md" not in visible and ".agents/skills" not in visible
    assert visible in generation_prompt(project)


def test_user_visible_generation_prompt_uses_saved_edit():
    from backend.domain.generation import user_generation_prompt

    edited = "担当者が期限切れをすぐ見つけられる一覧を作ってください。"
    project = ProjectInput(**INPUT, generation_prompt=edited)

    assert user_generation_prompt(project) == edited
    assert edited in generation_prompt(project)


def test_generation_rules_do_not_create_sample_data_or_log_raw_values():
    from backend.domain.generation import conventions

    rules = conventions()
    prompt = generation_prompt(ProjectInput(**INPUT))
    assert "サンプルデータ" in rules and "作らない" in rules
    assert "セル値" in rules and "ログへ出さない" in rules
    assert "do not create sample data" in prompt
    assert "never print or log file contents" in prompt

    visual = generation_prompt(ProjectInput(**INPUT, creation_profile={
        "app_pattern": "file_import", "work_category": "other", "app_type": "both",
        "goals": ["reporting"],
    }))
    browser = generation_prompt(ProjectInput(**INPUT, creation_profile={
        "app_pattern": "local_file_visualization", "work_category": "manufacturing",
        "app_type": "visualization", "goals": ["production_performance"],
    }))
    assert "never print or log file contents" in visual
    assert "never print file contents" in browser and "browser console" in browser
    assert "reference screen images" in visual and "reference screen images" in browser


def test_generation_spec_uses_app_forge_identity_without_a_login_screen():
    result = specification(ProjectInput(**INPUT))
    assert "ログイン" not in result["screens"]
    assert "X-Forge" in result["authentication"]
    assert "ログイン画面を作らない" in result["authentication"]


def interview_with(tmp_path, bridge_class):
    async def run():
        created = []
        def factory(*args, **kwargs):
            bridge = bridge_class(*args, **kwargs)
            created.append(bridge)
            return bridge
        agent = Agent(settings(tmp_path), factory)
        normal = await agent.connect()
        await agent.start_interview(PROJECT_ID, ProjectInput(**INPUT))
        await agent.task
        interview = next(bridge for bridge in created if bridge is not normal)
        state = agent.interview_status(PROJECT_ID)
        await agent.close()
        return state, interview
    return asyncio.run(run())


def test_the_interview_still_runs_without_plan_mode(tmp_path):
    """planモードが無い版でも聞き取りは成立する。ここで止めると機能ごと使えなくなる。

    この接続は working_directory を渡していないので read-only で開いており、
    モードが無くてもファイルの作成やコマンドの実行はできない。
    """
    class NoPlanMode(FakeBridge):
        async def call(self, method, params):
            if method == "collaborationMode/list":
                raise RuntimeError("Codexが要求を受け付けませんでした。")
            return await super().call(method, params)

    state, interview = interview_with(tmp_path, NoPlanMode)
    assert state["status"] == "completed"
    assert state["result"]["name"] == INPUT["name"]
    turn = next(payload for method, payload in interview.calls if method == "turn/start")
    assert "collaborationMode" not in turn


def test_a_refused_plan_mode_is_retried_as_a_plain_turn(tmp_path):
    class RefusesMode(FakeBridge):
        async def call(self, method, params):
            if method == "turn/start" and "collaborationMode" in params:
                self.calls.append((method, params))
                raise RuntimeError("Codexが要求を受け付けませんでした。")
            return await super().call(method, params)

    state, interview = interview_with(tmp_path, RefusesMode)
    assert state["status"] == "completed"
    starts = [payload for method, payload in interview.calls if method == "turn/start"]
    # 1度だけやり直す。2回目はモードを付けない。
    assert len(starts) == 2 and "collaborationMode" not in starts[1]


def test_a_failed_plan_turn_is_retried_as_a_plain_turn(tmp_path):
    """planモードはその版・そのアカウントで使えないことがある。

    受け付けられた（turn/startは成功した）のにターンごと失敗する場合も、
    素のターンでやり直す。モード指定だけが原因のときに、ヒアリング全体を
    諦めることにならないようにする。
    """
    class PlanTurnFails(FakeBridge):
        async def call(self, method, params):
            if method == "turn/start" and "collaborationMode" in params:
                self.calls.append((method, params))
                await self.notifications.put({"method": "turn/completed", "params": {
                    "threadId": "thread1", "turnId": "turn1",
                    "turn": {"id": "turn1", "status": "failed", "error": "model unavailable"}}})
                return {"turn": {"id": "turn1"}}
            return await super().call(method, params)

    state, interview = interview_with(tmp_path, PlanTurnFails)
    assert state["status"] == "completed"
    starts = [payload for method, payload in interview.calls if method == "turn/start"]
    assert len(starts) == 2 and "collaborationMode" not in starts[1]


USAGE_LIMIT = ("{'message': \"You've hit your usage limit. Upgrade to Pro or try again at "
               "Sep 19th, 2026 8:12 AM.\", 'codexErrorInfo': 'usageLimitExceeded'}")


def test_a_usage_limit_is_named_and_not_retried(tmp_path):
    """枠切れはやり直しても直らない。2度目は枠を無駄に使うだけになる。

    「要件を整理できませんでした。もう一度開始するか…」と出すと、直らない操作を
    何度も試させることになる。待てばよいのか、別の生成元へ移ればよいのかを伝える。
    """
    class OutOfCredit(FakeBridge):
        async def call(self, method, params):
            if method == "turn/start":
                self.calls.append((method, params))
                await self.notifications.put({"method": "turn/completed", "params": {
                    "threadId": "thread1", "turnId": "turn1",
                    "turn": {"id": "turn1", "status": "failed", "error": USAGE_LIMIT}}})
                return {"turn": {"id": "turn1"}}
            return await super().call(method, params)

    state, interview = interview_with(tmp_path, OutOfCredit)
    assert state["status"] == "failed"
    assert "利用枠の上限" in state["error"]
    # いつ戻るかが分かれば、待つ判断ができる。
    assert "Sep 19th, 2026 8:12 AM" in state["error"]
    assert "Gemini" in state["error"]
    # モードを外してやり直しても同じ結果になる。1回で止める。
    assert len([m for m, _ in interview.calls if m == "turn/start"]) == 1


def test_generation_names_the_usage_limit_too(tmp_path):
    async def run():
        class OutOfCredit(FakeBridge):
            async def call(self, method, params):
                if method == "turn/start":
                    self.calls.append((method, params))
                    await self.notifications.put({"method": "turn/completed", "params": {
                        "threadId": "thread1",
                        "turn": {"id": "turn1", "status": "failed", "error": USAGE_LIMIT}}})
                    return {"turn": {"id": "turn1"}}
                return await super().call(method, params)

        agent = Agent(settings(tmp_path), OutOfCredit)
        payload = GenerationInput(job_id=uuid4(), project_id=PROJECT_ID, specification=INPUT)
        await agent.start_generation(payload)
        await agent.task
        status = agent.job_status(payload.job_id)
        await agent.close()
        return status

    status = asyncio.run(run())
    assert status["status"] == "failed"
    assert status["failure_code"] == "usage_limit"
    assert "利用枠の上限" in status["error"] and "Gemini" in status["error"]


def test_an_interview_failure_says_which_kind_of_failure_it_was(tmp_path):
    """「もう一度開始するか省略してください」だけでは、待てば直るのか分からない。"""
    from backend.core.codex_bridge import CodexTransportClosed

    class Disconnects(FakeBridge):
        async def call(self, method, params):
            if method == "thread/start":
                raise CodexTransportClosed("Codex接続が終了しました。")
            return await super().call(method, params)

    state, _ = interview_with(tmp_path, Disconnects)
    assert state["status"] == "failed"
    assert state["error"] == INTERVIEW_ERRORS["transport"]
    assert "接続が切れました" in state["error"]


def test_the_interview_accepts_json_wrapped_in_prose_or_fences(tmp_path):
    """「JSONだけ返せ」と指示しても、前置きやコードフェンスは付いてくる。

    中身は正しいのに形だけでやり直しにすると、ヒアリングが通らない。
    """
    class Chatty(FakeBridge):
        async def call(self, method, params):
            if method == "turn/start":
                self.calls.append((method, params))
                result = ProjectInput(**INPUT).model_dump_json()
                await self.notifications.put({"method": "item/completed", "params": {
                    "threadId": "thread1", "turnId": "turn1", "item": {
                        "type": "agentMessage", "phase": "final_answer",
                        "text": f"確認しました。\n```json\n{result}\n```\n以上です。"}}})
                await self.notifications.put({"method": "turn/completed", "params": {
                    "threadId": "thread1", "turn": {"id": "turn1", "status": "completed"}}})
                return {"turn": {"id": "turn1"}}
            return await super().call(method, params)

    state, _ = interview_with(tmp_path, Chatty)
    assert state["status"] == "completed"
    assert state["result"]["name"] == INPUT["name"]


def test_requirements_interview_uses_a_separate_bridge(tmp_path):
    async def run():
        created = []
        def factory(*args, **kwargs):
            bridge = FakeBridge(*args, **kwargs)
            created.append(bridge)
            return bridge
        agent = Agent(settings(tmp_path), factory)
        normal = await agent.connect()
        await agent.start_interview(PROJECT_ID, ProjectInput(**INPUT))
        await agent.task
        assert agent.bridge is normal
        assert agent.interview_bridge is None
        state = agent.interview_status(PROJECT_ID)
        assert state["status"] == "completed"
        assert state["result"]["name"] == INPUT["name"]
        interview = next(bridge for bridge in created if bridge is not normal)
        started = next(payload for method, payload in interview.calls if method == "thread/start")
        instructions = started["developerInstructions"]
        # 学習を始めるまでの負担を抑え、初版に欠かせないことだけ確認する。
        assert "at most twice" in instructions and "no more than 3" in instructions
        assert "Do not exhaustively ask" in instructions
        # ただし、聞くべきでないことは聞かせない。
        assert "Stop immediately if no essential question remains" in instructions
        assert "hosting, frameworks, styling or deployment" in instructions
        assert "Do not ask about login" in instructions
        turn = next(payload for method, payload in interview.calls if method == "turn/start")
        assert "outputSchema" not in turn
        await agent.close()
    asyncio.run(run())


def test_concurrent_codex_connection_uses_one_initialized_bridge(tmp_path):
    async def run():
        created = []
        class SlowBridge(FakeBridge):
            async def __aenter__(self):
                await asyncio.sleep(0.01)
                return self
        def factory(*args, **kwargs):
            bridge = SlowBridge(*args, **kwargs)
            created.append(bridge)
            return bridge
        agent = Agent(settings(tmp_path), factory)
        first, second = await asyncio.gather(agent.connect(), agent.connect())
        assert first is second
        assert len(created) == 1
        await agent.close()
    asyncio.run(run())


def test_status_during_generation_keeps_the_job_bridge(tmp_path):
    """生成中の接続確認が、実行中のターンの接続を張り替えたり増やしたりしない。"""
    async def run():
        created = []
        class SlowBridge(FakeBridge):
            # 実物と同じ手順を踏む。起動し終えるまでプロセスは無く、停止にも間がある。
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.process = None

            async def __aenter__(self):
                await asyncio.sleep(0.01)
                self.process = SimpleNamespace(returncode=None)
                return self

            async def __aexit__(self, *args):
                await asyncio.sleep(0)
                if self.process:
                    self.process.returncode = 0
        def factory(*args, **kwargs):
            bridge = SlowBridge(*args, **kwargs)
            bridge.working_directory = kwargs.get("working_directory")
            created.append(bridge)
            return bridge
        agent = Agent(settings(tmp_path), factory)
        payload = GenerationInput(job_id=uuid4(), project_id=PROJECT_ID, specification=INPUT)
        polls = []
        async def poll():
            while agent.task is None or not agent.task.done():
                polls.append(await agent.status())
                await asyncio.sleep(0)
        poller = asyncio.create_task(poll())
        await agent.start_generation(payload)
        await agent.task
        await poller
        assert agent.job_status(payload.job_id)["status"] == "generated"
        turning = [bridge for bridge in created if any(m == "turn/start" for m, _ in bridge.calls)]
        # ターンを持てるのは、このジョブ用の権限で開いた接続だけ。
        assert len(turning) == 1
        assert turning[0] is agent.bridge
        assert turning[0].working_directory == agent.workspace_path(payload.project_id)
        # 生成中の確認は直前に見た本人情報を返すだけで、接続を作り直さない。
        assert polls and all(state["email"] == "person@example.test" for state in polls)
        assert any(state["busy"] for state in polls)
        # 捨てた接続は必ず閉じる。取り残したプロセスを作らない。
        alive = [bridge for bridge in created
                 if bridge.process and bridge.process.returncode is None]
        assert alive == [agent.bridge]
        await agent.close()
    asyncio.run(run())


def test_transport_loss_is_not_reported_as_api_incompatibility(tmp_path):
    async def run():
        class ClosingBridge(FakeBridge):
            async def call(self, method, params):
                if method == "turn/start":
                    self.calls.append((method, params))
                    await self.notifications.put({"method": "transport/closed"})
                    return {"turn": {"id": "turn1"}}
                return await super().call(method, params)
        agent = Agent(settings(tmp_path), ClosingBridge)
        payload = GenerationInput(job_id=uuid4(), project_id=PROJECT_ID, specification=INPUT)
        await agent.start_generation(payload)
        await agent.task
        status = agent.job_status(payload.job_id)
        assert status["status"] == "failed"
        assert status["failure_code"] == "transport"
        assert "[transport]" in status["error"]
        await agent.close()
    asyncio.run(run())


def test_generate_idempotent_permission_profile_and_bundle(tmp_path):
    async def run():
        agent = Agent(settings(tmp_path), FakeBridge)
        payload = GenerationInput(job_id=uuid4(), project_id=PROJECT_ID, specification=INPUT)
        await agent.start_generation(payload)
        await agent.task
        status = agent.job_status(payload.job_id)
        assert status["status"] == "generated"
        # 宣言ターンと実装ターンの2回ぶん。段階を分けたぶんの消費を隠さない。
        assert status["usage"] == {"input_tokens": 180, "output_tokens": 20,
                                   "cached_tokens": 80, "total_tokens": 200}
        await agent.start_generation(payload)
        calls = agent.bridge.calls
        starts = [p for m, p in calls if m == "turn/start"]
        assert len(starts) == 2
        # 1回目は依存の宣言だけ。ここで挙げ漏らすと、サンドボックスでは足せない。
        assert "write ONLY the two dependency manifests" in starts[0]["input"][0]["text"]
        turn = next(p for m, p in calls if m == "turn/start")
        assert "sandboxPolicy" not in turn
        thread = next(p for m, p in calls if m == "thread/start")
        # 対話で直していくため、作業場所はプロジェクト単位で固定する。
        assert thread["cwd"].endswith(f"/projects/{payload.project_id}/workspace")
        assert not thread["cwd"].startswith(str(agent.job_path(payload.job_id)))
        args = generation_permission_args(thread["cwd"])
        assert 'default_permissions="app_forge_job"' in args
        assert any(value.startswith("permissions.app_forge_job=") and "enabled=false" in value
                   for value in args)
        workspace = agent.workspace_path(payload.project_id)
        # 規約はワークスペースに置かれ、成果物には入らない。
        assert (workspace / "AGENTS.md").read_text() == conventions()
        bundle = CodeBundle.model_validate_json((agent.job_path(payload.job_id) / "bundle.json").read_text())
        assert "AGENTS.md" not in {f.path for f in bundle.files}
        await agent.close()
    asyncio.run(run())


def test_restart_fails_without_auto_inference(tmp_path):
    agent = Agent(settings(tmp_path), FakeBridge)
    job_id = uuid4()
    agent.write_status(job_id, "generating")
    assert agent.job_status(job_id)["status"] == "failed"
    assert agent.bridge is None


def test_login_single_attempt_and_status_polling(tmp_path):
    async def run():
        agent = Agent(settings(tmp_path), FakeBridge)
        bridge = await agent.connect()
        bridge.account_type = None
        first = await agent.start_login()
        assert first == await agent.start_login()
        assert (await agent.status())["status"] == "pending"
        assert len([m for m, _ in bridge.calls if m == "account/login/start"]) == 1
        bridge.account_type = "chatgpt"
        bridge.login_done.set()
        await agent.task
        assert (await agent.status())["status"] == "connected"
        await agent.close()
    asyncio.run(run())


def test_agent_has_no_public_auth_or_arbitrary_rpc(tmp_path):
    app = create_agent(settings(tmp_path))
    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
        assert client.get("/account").status_code == 401
        assert client.post("/login", json={}).status_code == 401
        assert client.post("/rpc", json={"method": "account/read"}).status_code == 401


def test_fixed_kubernetes_isolation():
    config = ControllerSettings(token="test-only-" + "x" * 40,
        agent_image="registry.example.com/koyorina-agent@sha256:" + "a" * 64)
    first, second = resources(uuid4(), config), resources(uuid4(), config)
    assert first["pods"]["metadata"]["name"] != second["pods"]["metadata"]["name"]
    pod = first["pods"]["spec"]
    assert pod["automountServiceAccountToken"] is False
    assert pod["securityContext"]["runAsNonRoot"] is True
    assert pod["nodeSelector"] == {"kubernetes.io/hostname": "k3s-agent-2", "kubernetes.io/arch": "amd64"}
    assert pod["securityContext"]["seccompProfile"] == {
        "type": "Localhost", "localhostProfile": "koyorina/codex-bwrap-amd64-v1.json"}
    assert pod["containers"][0]["securityContext"]["allowPrivilegeEscalation"] is False
    assert not any("hostPath" in volume for volume in pod["volumes"])
    # 監査相関IDを含む。秘密はtokenのみで、値は直接書かない。
    env = {item["name"]: item for item in pod["containers"][0]["env"]}
    assert set(env) == {"AGENT_USER_ID", "AGENT_TOKEN", "AGENT_CODEX_MODEL", "AGENT_CODEX_EFFORT",
                        "AGENT_GENERATOR", "AGENT_GEMINI_MODEL", "AGENT_ENVIRONMENT",
                        "AGENT_TENANT_ID", "AGENT_POD_NAME", "AGENT_NAMESPACE",
                        "AUDIT_LOG_REQUEST_CONTENT"}
    assert "value" not in env["AGENT_TOKEN"] and "valueFrom" in env["AGENT_TOKEN"]
    assert env["AGENT_CODEX_MODEL"]["value"] == "" and env["AGENT_CODEX_EFFORT"]["value"] == ""
    assert env["AUDIT_LOG_REQUEST_CONTENT"]["value"] == "0"


def test_rpc_dispatcher_handles_concurrent_login_and_status(tmp_path):
    async def run():
        bridge = CodexBridge("unused", tmp_path, str(uuid4()), allow_login=True)
        reader = asyncio.StreamReader()
        bridge.process = SimpleNamespace(stdout=reader)
        sent = []
        async def send(message):
            sent.append(message)
        bridge.send = send
        read_task = asyncio.create_task(bridge._read())
        request = asyncio.create_task(bridge.call("account/read", {}))
        login = asyncio.create_task(bridge.wait_for_login("login1"))
        await asyncio.sleep(0)
        reader.feed_data((json.dumps({"method": "account/login/completed", "params": {"loginId": "login1", "success": True}}) + "\n").encode())
        reader.feed_data(b'{"id":1,"result":{"account":null}}\n')
        assert await login is True
        assert await request == {"account": None}
        reader.feed_eof()
        await read_task
        with pytest.raises(ValueError):
            await bridge.call("thread/start", {})
    asyncio.run(run())


def test_conventions_reach_the_workspace_and_stay_out_of_the_artifact(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "AGENTS.md").write_text(conventions())
    (workspace / "APP-FORGE-SPEC.json").write_text('{"name": "x"}')
    for source in BUNDLE["files"]:
        target = workspace / source["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source["content"])
    bundle = code_bundle_from_workspace(workspace)
    assert {f.path for f in bundle.files} == {f["path"] for f in BUNDLE["files"]}
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("AGENTS.md", conventions())
        archive.writestr("APP-FORGE-SPEC.json", '{"name": "x"}')
        for source in BUNDLE["files"]:
            archive.writestr(source["path"], source["content"])
    assert {f.path for f in code_bundle_from_zip(buffer.getvalue()).files} == {f["path"] for f in BUNDLE["files"]}


def test_generation_skills_are_installed_in_the_workspace_convention_path(tmp_path, monkeypatch):
    """置き場は作業場所の中の `.agents/skills`。

    そこならCodexが見つける場所の慣習に合い、かつサンドボックスからそのまま
    読める（資産のコピーを指示するスキルがあり、読めないと手順が成立しない）。
    成果物と変更履歴からは名前で外す。
    """
    from backend.core.codex_bridge import skills_directory
    from backend.domain.generation import install_generation_skills

    source = tmp_path / "source-skills"
    skill = source / "example-skill"
    (skill / "references").mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: example-skill\ndescription: test\n---\n")
    (skill / "references" / "guide.md").write_text("guide")
    monkeypatch.setenv("GENERATION_SKILLS_ROOT", str(source))

    workspace = tmp_path / "projects" / str(uuid4()) / "workspace"
    workspace.mkdir(parents=True)
    root = skills_directory(workspace)
    assert root == workspace / ".agents" / "skills"
    root.parent.mkdir(parents=True)
    install_generation_skills(root)
    installed = root / "example-skill"
    assert (installed / "SKILL.md").is_file()
    assert (installed / "references" / "guide.md").read_text() == "guide"

    # 正から消えたファイルを残さない。
    (skill / "references" / "guide.md").unlink()
    install_generation_skills(root)
    assert not (installed / "references" / "guide.md").exists()

    # 提示用は CODEX_HOME に残す。Codexがスキルを見つけるのはこの場所で、
    # ここから出すと読めるようにはなってもモデルへ提示されない。
    # ただしサンドボックスへ許可は出さない（同じ場所に資格情報がある）。
    home, env = isolated_environment(tmp_path / "users", str(uuid4()))
    assert (home / "codex" / "skills" / "example-skill" / "SKILL.md").is_file()


def test_the_skills_are_inside_the_area_the_sandbox_can_already_read():
    """作業場所の中なので、読むための追加の許可が要らない。

    外に置くと許可を足すことになり、その許可先を一つ間違えると
    （CODEX_HOME のように）資格情報まで読める位置になる。
    """
    import tempfile
    from backend.core.codex_bridge import skills_directory
    from backend.domain.generation import generation_permission_args

    with tempfile.TemporaryDirectory() as directory:
        workspace = Path(directory) / "workspace"
        workspace.mkdir()
        skills = skills_directory(workspace)
        assert skills.is_relative_to(workspace)
        profile = next(part for part in generation_permission_args(str(workspace))
                       if part.startswith("permissions."))
        assert f'{json.dumps(str(workspace.resolve()))}="write"' in profile
        # 追加の許可は何も出していない。
        assert profile.count('="read"') == 1 and '":minimal"="read"' in profile


def test_model_and_effort_are_validated_and_sent(tmp_path):
    """モデルと推論の深さは検証してから渡す。未指定ならCodexの既定に任せる。"""
    for bad in ({"model": "../etc/passwd"}, {"effort": "unlimited"}, {"model": "A" * 60}):
        with pytest.raises(ValueError):
            model_settings(bad.get("model", ""), bad.get("effort", ""))
    assert model_settings("", "") == {}
    assert model_settings("gpt-5.6-terra", "high") == {"model": "gpt-5.6-terra", "effort": "high"}

    async def run():
        base = settings(tmp_path)
        agent = Agent(base.model_copy(update={"codex_model": "gpt-5.6-terra", "codex_effort": "high"}),
                      FakeBridge)
        payload = GenerationInput(job_id=uuid4(), project_id=PROJECT_ID, specification=INPUT)
        await agent.start_generation(payload)
        await agent.task
        calls = dict(agent.bridge.calls)
        # スレッドはモデルだけ、ターンは深さも受け取る。
        assert calls["thread/start"]["model"] == "gpt-5.6-terra"
        assert "effort" not in calls["thread/start"]
        assert calls["turn/start"]["model"] == "gpt-5.6-terra" and calls["turn/start"]["effort"] == "high"
        await agent.close()
    asyncio.run(run())


def test_shared_worker_mounts_vertex_but_auth_worker_does_not():
    """The tenant worker can switch providers; the Codex auth worker stays isolated."""
    image = "registry.example.com/koyorina-agent@sha256:" + "b" * 64
    base = ControllerSettings(token="t" * 40, agent_image=image)
    codex_pod = resources(uuid4(), base)["pods"]["spec"]
    assert not [v for v in codex_pod["volumes"] if v["name"] == "vertex"]
    assert not [e for e in codex_pod["containers"][0]["env"] if e["name"].startswith("GOOGLE_")]

    gemini = base.model_copy(update={"vertex_project": "example-project-dev"})
    pod = resources(uuid4(), gemini, "codex")["pods"]["spec"]
    volume = [v for v in pod["volumes"] if v["name"] == "vertex"][0]
    assert volume["secret"]["secretName"] == "koyorina-vertex"
    mount = [m for m in pod["containers"][0]["volumeMounts"] if m["name"] == "vertex"][0]
    assert mount["mountPath"] == "/run/vertex" and mount["readOnly"] is True
    env = {e["name"]: e.get("value") for e in pod["containers"][0]["env"]}
    assert env["GOOGLE_GENAI_USE_VERTEXAI"] == "1" and env["GOOGLE_CLOUD_PROJECT"] == "example-project-dev"
    assert env["AUDIT_LOG_REQUEST_CONTENT"] == "0"
    # The default remains configurable, while each request can select either provider.
    assert env["AGENT_GENERATOR"] == "codex"
    auth = resources(uuid4(), gemini, "codex", auth_only=True)["pods"]["spec"]
    assert not any(v["name"] == "vertex" for v in auth["volumes"])
    assert not any(e["name"].startswith("GOOGLE_") for e in auth["containers"][0]["env"])
    # プロジェクトIDが無いままGemini経路を有効にできない。
    with pytest.raises(ValueError):
        ControllerSettings(token="t" * 40, agent_image=image, generator="gemini")


def test_shared_worker_reads_developer_api_key_only_from_secret():
    image = "registry.example.com/koyorina-agent@sha256:" + "c" * 64
    settings = ControllerSettings(token="t" * 40, agent_image=image, generator="gemini",
                                  gemini_api_backend="developer", vertex_project="")
    pod = resources(uuid4(), settings, tenant=str(uuid4()))["pods"]["spec"]
    assert not any(volume["name"] == "vertex" for volume in pod["volumes"])
    env = {item["name"]: item for item in pod["containers"][0]["env"]}
    assert env["GEMINI_API_BACKEND"]["value"] == "developer"
    # 環境のキーは画面で入れる運用だと配備時に無い。無くても Pod は起動させる。
    assert env["GEMINI_API_KEY"]["valueFrom"]["secretKeyRef"] == {
        "name": "koyorina-gemini-api", "key": "GEMINI_API_KEY", "optional": True}
    assert "value" not in env["GEMINI_API_KEY"]

    content_settings = settings.model_copy(update={"audit_log_request_content": True})
    content_pod = resources(uuid4(), content_settings, "gemini", tenant=str(uuid4()))["pods"]["spec"]
    content_env = {item["name"]: item.get("value") for item in content_pod["containers"][0]["env"]}
    assert content_env["AUDIT_LOG_REQUEST_CONTENT"] == "1"

    auth = resources(uuid4(), settings, "codex", auth_only=True)["pods"]["spec"]
    auth_env = {item["name"] for item in auth["containers"][0]["env"]}
    assert "GEMINI_API_KEY" not in auth_env and "GEMINI_API_BACKEND" not in auth_env


def test_antigravity_can_share_a_vertex_worker_with_a_developer_api_key():
    image = "registry.example.com/koyorina-agent@sha256:" + "d" * 64
    settings = ControllerSettings(token="t" * 40, agent_image=image,
                                  gemini_api_backend="vertex", vertex_project="test-project",
                                  antigravity_enabled=True)
    pod = resources(uuid4(), settings, "gemini", tenant=str(uuid4()))["pods"]["spec"]
    env = {item["name"]: item for item in pod["containers"][0]["env"]}
    assert env["GEMINI_API_BACKEND"]["value"] == "vertex"
    assert env["GEMINI_API_KEY"]["valueFrom"]["secretKeyRef"]["name"] == "koyorina-gemini-api"
    assert env["AGENT_ANTIGRAVITY_ENABLED"]["value"] == "true"


def test_scaffold_is_placed_once_and_kept_in_the_artifact(tmp_path):
    """社内共通の部品は初回だけ置く。上書きするとモデルの変更を巻き戻す。"""
    files = scaffold_files()
    assert "frontend/src/styles/tokens.css" in files
    assert "backend/core/security_headers.py" in files
    assert all(artifact_path_is_allowed(path) for path in files)

    async def run():
        agent = Agent(settings(tmp_path), FakeBridge)
        payload = GenerationInput(job_id=uuid4(), project_id=PROJECT_ID, specification=INPUT)
        await agent.start_generation(payload)
        await agent.task
        workspace = agent.workspace_path(payload.project_id)
        tokens = workspace / "frontend" / "src" / "styles" / "tokens.css"
        assert tokens.is_file() and "--ink-1" in tokens.read_text()
        # 規約と違い、共通部品は成果物として届ける。
        bundle = CodeBundle.model_validate_json((agent.job_path(payload.job_id) / "bundle.json").read_text())
        paths = {f.path for f in bundle.files}
        assert "frontend/src/styles/tokens.css" in paths and "AGENTS.md" not in paths

        # 2回目は置き直さない。
        tokens.write_text("/* モデルが直した内容 */")
        follow = GenerationInput(job_id=uuid4(), project_id=PROJECT_ID, specification=INPUT,
                                 instruction="一覧に絞り込みを足して")
        await agent.start_generation(follow)
        await agent.task
        assert tokens.read_text() == "/* モデルが直した内容 */"
        await agent.close()
    asyncio.run(run())


def test_required_files_include_the_dependency_list():
    """依存一覧が無いとアプリが起動しない。規約で必須にする。

    宣言は pyproject.toml ひとつ。requirements.txt へ写させると、片方だけ直した
    ものが必ず出てきて、入るのは写しでないほうになる。
    """
    rules = conventions()
    assert "ルートの `pyproject.toml`" in rules
    assert "uv sync --no-install-project" in rules and "uv run" in rules
    assert "`pyproject.yaml` や uv の YAML は存在しない" in rules
    assert "uv pip install -r pyproject.toml" in rules
    assert "ここに無いパッケージは、アプリが起動しない" in rules
    assert "`backend/requirements.txt` は書かない" in rules


def test_missing_pod_with_existing_disk_is_reported_as_preparing(tmp_path):
    """作業ディスクが残っていれば、その人は接続済みのことがある。未接続として扱わない。

    Podを作り直している最中に「接続できません」と赤字で返すと、操作が失敗したように見える。
    """
    import asyncio
    from backend.worker.controller import Provisioner
    settings = ControllerSettings(token="test-only-" + "x" * 40,
        agent_image="registry.example.com/koyorina-agent@sha256:" + "a" * 64)
    provisioner = Provisioner(settings)
    existing, created = {"persistentvolumeclaims"}, []

    async def kube(method, resource, name="", body=None):
        if method == "POST":
            created.append(resource)
            existing.add(resource)
            return {"metadata": {"name": name}}
        return {"metadata": {"name": name}} if resource in existing else None

    provisioner.kube = kube
    for path in ("/account", "/login"):
        result = asyncio.run(provisioner.relay(uuid4(), "GET", path))
        assert result["status"] == "preparing" and result["login"] is None
    assert "pods" in created  # 用意も始める。待っていれば繋がる、という案内にするため。

    # 使ったことが無い人は未接続のまま。作ってもいないPodを待たせない。
    existing.clear()
    assert asyncio.run(provisioner.relay(uuid4(), "GET", "/account"))["status"] == "disconnected"


def test_test_leftovers_do_not_fail_the_handover(tmp_path):
    """pytest の一時ファイルで、生成物ごと弾かれないこと。

    サンドボックスは /tmp に書けないので、Python は作業場所へ退避する。
    そのまま成果物として拾うと test.db のような扱えない名前で検査に落ち、
    生成そのものが失敗する（実際に本番で起きた）。
    """
    from backend.domain.generation import code_bundle_from_workspace, is_generated_leftover

    workspace = tmp_path / "workspace"
    for name, text in (("backend/main.py", "app = 1\n"),
                       ("pyproject.toml", PYPROJECT),
                       ("frontend/package.json",
                        '{"scripts": {"build": "vite build"}}\n'),
                       ("frontend/src/app.vue", "<template/>\n")):
        path = workspace / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    # pytest が残していく形をそのまま置く。
    leftover = workspace / "pytest-of-forge" / "pytest-0" / "test_entry_crud_0"
    leftover.mkdir(parents=True)
    (leftover / "test.db").write_bytes(b"SQLite format 3\x00")

    assert is_generated_leftover(("pytest-of-forge", "pytest-0", "test.db"))
    assert not is_generated_leftover(("backend", "main.py"))

    bundle = code_bundle_from_workspace(workspace)
    paths = [item.path for item in bundle.files]
    assert "backend/main.py" in paths
    assert not any(p.startswith("pytest-of-") for p in paths)


def test_a_broken_pyproject_is_reported_before_the_preview_installs_nothing():
    """依存の宣言は pyproject.toml ひとつ。プレビューはここからしか入れない。

    [project] が無い・dependencies が無い・fastapi を書き忘れた、のどれも
    「プレビューは起動したが import で落ちる」という形でしか表に出ない。
    どこが悪いのかが分からないので、生成の時点で名指しで返す。
    """
    from backend.domain.generation import pyproject_problems

    assert pyproject_problems(PYPROJECT) == []
    assert "TOMLとして読めません" in pyproject_problems("name: ledger\n")[0]
    assert "[project]テーブルがありません" in pyproject_problems('[tool.uv]\nmanaged = true\n')[0]
    assert "dependencies" in pyproject_problems('[project]\nname = "x"\n')[0]
    # extras や版指定が付いていても、名前で見る。
    assert pyproject_problems('[project]\nname = "x"\n'
                              'dependencies = ["uvicorn[standard]>=0.34", "fastapi"]\n') == []
    missing = pyproject_problems('[project]\nname = "x"\ndependencies = ["fastapi"]\n')
    assert "uvicorn" in missing[0] and "fastapi" not in missing[0]


def test_the_previous_conventions_dependency_list_still_starts_a_preview():
    """requirements.txt しか持たない既存アプリを、規約の変更で起動できなくしない。"""
    script = (Path(__file__).resolve().parents[2] / "setup/preview/entrypoint.sh").read_text()
    assert "uv pip install --quiet -r pyproject.toml" in script
    assert "uv pip install --quiet -r backend/requirements.txt" in script


def test_failing_checks_come_back_as_a_repair_turn_with_the_real_output(tmp_path, monkeypatch):
    """検査が落ちたら、本物の出力をそのまま返して直させる。

    利用者はコードを読めないので、「プレビューで落ちたら自分で直して依頼する」は
    成立しない。落ちた内容は、利用者を経由せずモデルへ戻す。
    """
    from backend.worker import toolchain_runner

    rounds = []

    async def verify(workspace, report=None, cache=None):
        rounds.append(len(rounds))
        # 直すたびに出力は変わる。同じままなら「進んでいない」として打ち切る。
        return [f"$ vite build\n(終了コード 1)\nsrc/app.vue(1{len(rounds)},3): error TS2304"] \
            if len(rounds) < 3 else []

    monkeypatch.setattr(toolchain_runner, "verify", verify)
    monkeypatch.setattr(toolchain_runner, "install", lambda *a, **k: _none())

    async def run():
        agent = Agent(settings(tmp_path), FakeBridge)
        payload = GenerationInput(job_id=uuid4(), project_id=PROJECT_ID, specification=INPUT)
        await agent.start_generation(payload)
        await agent.task
        assert agent.job_status(payload.job_id)["status"] == "generated"
        prompts = [p["input"][0]["text"] for m, p in agent.bridge.calls if m == "turn/start"]
        # 宣言・実装・修正2回。
        assert len(prompts) == 4
        repairs = [text for text in prompts if "were run by Koyorina" in text]
        assert len(repairs) == 2
        # 要約せずに渡す。どのファイルの何行目かが消えると直せない。
        assert "src/app.vue(11,3): error TS2304" in repairs[0]
        await agent.close()
    asyncio.run(run())
    assert len(rounds) == 3


async def _none():
    return []


def test_checks_that_never_pass_still_hand_over_the_code_with_a_warning(tmp_path, monkeypatch):
    """直しきれなくても、書けたものは渡す。動かないかもしれないことは伝える。"""
    from backend.worker import toolchain_runner

    monkeypatch.setattr(toolchain_runner, "install", lambda *a, **k: _none())

    async def verify(workspace, report=None, cache=None):
        return ["$ pytest\n(終了コード 1)\nFAILED test_app.py::test_total"]

    monkeypatch.setattr(toolchain_runner, "verify", verify)

    async def run():
        agent = Agent(settings(tmp_path), FakeBridge)
        payload = GenerationInput(job_id=uuid4(), project_id=PROJECT_ID, specification=INPUT)
        await agent.start_generation(payload)
        await agent.task
        assert agent.job_status(payload.job_id)["status"] == "generated"
        events = (agent.job_path(payload.job_id) / "progress.json").read_text()
        assert "検査が通らないまま残った箇所があります" in events
        await agent.close()
    asyncio.run(run())


def test_install_failures_are_handed_to_the_model_rather_than_silently_ignored(tmp_path, monkeypatch):
    """入らなかった部品を黙って進めると、動かない理由が「書き方が悪い」に見える。"""
    from backend.worker import toolchain_runner

    async def install(workspace, packages, report=None, cache=None):
        return ["$ uv pip install\n(終了コード 1)\nNo wheel available for weasyprint"]

    monkeypatch.setattr(toolchain_runner, "install", install)
    monkeypatch.setattr(toolchain_runner, "verify", lambda *a, **k: _none())

    async def run():
        agent = Agent(settings(tmp_path), FakeBridge)
        payload = GenerationInput(job_id=uuid4(), project_id=PROJECT_ID, specification=INPUT)
        await agent.start_generation(payload)
        await agent.task
        prompts = [p["input"][0]["text"] for m, p in agent.bridge.calls if m == "turn/start"]
        assert any("No wheel available for weasyprint" in text for text in prompts)
        await agent.close()
    asyncio.run(run())


def test_a_failing_repair_turn_does_not_throw_away_the_generated_code(tmp_path, monkeypatch):
    """修正ターンで枠が切れることがある。書けていたものまで捨てない。"""
    from backend.worker import toolchain_runner

    monkeypatch.setattr(toolchain_runner, "install", lambda *a, **k: _none())

    async def verify(workspace, report=None, cache=None):
        return ["$ pytest\n(終了コード 1)\nFAILED"]

    monkeypatch.setattr(toolchain_runner, "verify", verify)

    async def run():
        agent = Agent(settings(tmp_path), FakeBridge)
        payload = GenerationInput(job_id=uuid4(), project_id=PROJECT_ID, specification=INPUT)
        original = agent.codex_turn
        calls = []

        async def flaky(*args, **kwargs):
            calls.append(kwargs.get("text"))
            if "were run by Koyorina" in (kwargs.get("text") or ""):
                raise RuntimeError("usage limit")
            return await original(*args, **kwargs)

        agent.codex_turn = flaky
        await agent.start_generation(payload)
        await agent.task
        assert agent.job_status(payload.job_id)["status"] == "generated"
        # 1回試して落ちたら、そこで打ち切る。同じ失敗を繰り返さない。
        assert sum("were run by Koyorina" in (t or "") for t in calls) == 1
        await agent.close()
    asyncio.run(run())


def test_the_manifest_turn_does_not_depend_on_the_model_opening_agents_md():
    """`codex app-server` は AGENTS.md を自動では読み込まない（対話CLIの挙動）。

    実装ターンは中身が規約に依存するので、モデルはたいてい自分で開く。宣言ターンは
    「マニフェスト2つだけ書け」という小さい依頼なので、開かずに済ませることがある。
    実際に「AGENTS.md の内容は追加表示されませんでした」と言って進んだ。
    構成を取り違えた依存一覧で導入してしまうので、決め手になる事実は依頼文へ直接書く。
    """
    from backend.domain.generation import manifest_prompt
    from backend.domain.projects import ProjectInput
    from backend.tests.test_projects import INPUT

    text = manifest_prompt(ProjectInput.model_validate(INPUT))
    assert "does not inject it automatically" in text
    for fixed in ("Python 3.14", "FastAPI", "SQLAlchemy", "Vue 3", "Vuetify 3", "Vite"):
        assert fixed in text, f"{fixed} が宣言ターンの依頼文に無い"


def test_every_job_records_what_was_installed_even_when_nothing_was_reinstalled(tmp_path, monkeypatch):
    """導入を飛ばした回にも記録を残す。

    宣言が前回と同じなら入れ直さない。そこで記録も飛ばすと、残るのは構成を変えた
    回だけになり、「このジョブの時点で何が入っていたか」を追えなくなる。
    """
    from backend.worker import toolchain_runner

    monkeypatch.setattr(toolchain_runner, "inventory",
                        lambda workspace, cache=None: _listing())

    async def run():
        agent = Agent(settings(tmp_path), FakeBridge)
        project_id = uuid4()
        workspace = agent.workspace_path(project_id)
        workspace.mkdir(mode=0o700, parents=True, exist_ok=True)
        (workspace / ".venv").mkdir()
        # 前回と同じ宣言。入れ直さない経路に入る。
        marker = agent.dependency_marker(project_id)
        marker.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        marker.write_text(dependency_digest(workspace), encoding="utf-8")

        payload = GenerationInput(job_id=uuid4(), project_id=project_id, specification=INPUT)
        agent.job_path(payload.job_id).mkdir(mode=0o700, parents=True, exist_ok=True)
        assert await agent.prepare_dependencies(payload, workspace, _Silent()) == []
        recorded = (agent.job_path(payload.job_id) / "dependencies.txt").read_text()
        assert "fastapi==" in recorded
        await agent.close()
    asyncio.run(run())


async def _listing():
    return "# uv pip freeze\nfastapi==0.141.1\n"


class _Silent:
    def record(self, *args, **kwargs):
        pass


def test_token_counts_survive_a_change_in_the_notification_shape():
    """消費の通知の形は、CLIの版で変わる。外したら0ではなく、拾える形を広く見る。

    0.155.1 へ上げた直後から usage が空になった。生成は成功しているのに画面の
    消費が0、という出方をする。条件を固く書いていたのが原因なので、名前の揺れと
    入れ子の有無を1か所で吸収する。
    """
    from backend.worker.agent import token_counts

    nested = {"total": {"inputTokens": 5, "outputTokens": 2, "cachedInputTokens": 1,
                        "totalTokens": 7}}
    assert token_counts(nested, "total") == {"input_tokens": 5, "output_tokens": 2,
                                             "cached_tokens": 1, "total_tokens": 7}
    # 入れ子が無くなった版。直下に並んでいても拾う。
    assert token_counts({"inputTokens": 5, "totalTokens": 7}, "total") == {
        "input_tokens": 5, "total_tokens": 7}
    # snake_case でも、別名（prompt/completion）でも拾う。
    assert token_counts({"prompt_tokens": 3, "completion_tokens": 1}, "total") == {
        "input_tokens": 3, "output_tokens": 1}
    # 取れないときは空。0を詰めて「消費0」に見せない。
    assert token_counts({"total": {"somethingElse": 9}}, "total") == {}
    assert token_counts({"total": {"inputTokens": 0, "totalTokens": 0}}, "total") == {}
    assert token_counts(None, "total") == {} and token_counts("x", "total") == {}


def test_usage_is_counted_once_per_turn_even_though_totals_are_cumulative(tmp_path):
    """通知は累計。差を取らずに足すと、ターンが進むほど水増しされる。

    段階を分けたので、同じスレッドで複数のターンが続くようになった。
    """
    async def run():
        agent = Agent(settings(tmp_path), FakeBridge)
        payload = GenerationInput(job_id=uuid4(), project_id=uuid4(), specification=INPUT)
        await agent.start_generation(payload)
        await agent.task
        usage = agent.job_status(payload.job_id)["usage"]
        # 宣言ターンと実装ターンで、累計は 100 → 200。消費は各100。
        assert usage["total_tokens"] == 200
        await agent.close()
    asyncio.run(run())


def test_unhandled_item_types_are_reported_instead_of_dropped(tmp_path, caplog):
    """扱っていない種類のitemを黙って捨てない。

    拾っているのは agentMessage / commandExecution / fileChange の3つだけ。
    それ以外は記録にも画面にも残らないので、「スキルが使われていない」のか
    「見えていないだけ」なのかを区別できなかった。種類と鍵だけ残す。
    """
    import logging

    class SkillBridge(FakeBridge):
        async def call(self, method, params=None):
            result = await super().call(method, params)
            if method == "turn/start":
                await self.notifications.put({"method": "item/completed", "params": {
                    "threadId": "thread1", "turnId": "turn1",
                    "item": {"type": "skillInvocation", "id": "s1", "name": "webapp-scaffold"}}})
            return result

    async def run():
        agent = Agent(settings(tmp_path), SkillBridge)
        payload = GenerationInput(job_id=uuid4(), project_id=uuid4(), specification=INPUT)
        with caplog.at_level(logging.INFO, logger="uvicorn.error"):
            await agent.start_generation(payload)
            await agent.task
        await agent.close()
    asyncio.run(run())
    reported = [r.getMessage() for r in caplog.records if "turn_item_unhandled" in r.getMessage()]
    assert reported and "skillInvocation" in reported[0]
    # 中身は出さない。種類と鍵だけ。
    assert "webapp-scaffold" not in reported[0]


def test_skills_point_at_the_directory_app_forge_actually_provides():
    """スキルは自分の資産をコピーさせる。置き場を取り違えると手順が成立しない。

    元は `~/.claude/skills/...` を指していた。Koyorina にはその場所が無く、
    仮にあってもサンドボックスの外なので読めない。資産のコピーで必ず失敗する。
    """
    root = Path(__file__).resolve().parents[2] / "Skills"
    documents = sorted(root.glob("*/SKILL.md"))
    assert documents, "Skillsを読めていない"
    for document in documents:
        text = document.read_text(encoding="utf-8")
        for line in text.splitlines():
            if line.startswith("SKILL_DIR"):
                assert "APP_FORGE_SKILLS" in line, f"{document.parent.name}: {line}"
        # 存在しない場所を直に指さない。
        assert "~/.claude/skills" not in text, document.parent.name


def test_the_conventions_tell_the_model_where_the_skills_are():
    """置き場を規約にも書く。環境変数が渡らない経路でも、読める場所が分かるように。"""
    rules = conventions()
    assert "$APP_FORGE_SKILLS" in rules and ".agents/skills/" in rules
    assert "編集・移動・削除しないこと" in rules


def test_skills_are_both_discoverable_and_readable(tmp_path, monkeypatch):
    """提示と読み取りで置き場が違う。片方だけでは足りない。

    CODEX_HOME はCodexがスキルを見つける場所だが、サンドボックスの外なので
    資産を読めない。作業場所の隣は読めるが、そこだけに置くとモデルへ提示されない。
    どちらの用途にも同じ内容を配る。サンドボックスへ許可を出すのは後者だけで、
    資格情報のある CODEX_HOME には出さない。
    """
    from backend.core.codex_bridge import isolated_environment, skills_directory
    from backend.domain.generation import install_generation_skills

    source = tmp_path / "source-skills"
    skill = source / "example-skill"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: example-skill\ndescription: test\n---\n")
    monkeypatch.setenv("GENERATION_SKILLS_ROOT", str(source))

    home, env = isolated_environment(tmp_path / "users", str(uuid4()))
    workspace = tmp_path / "projects" / str(uuid4()) / "workspace"
    workspace.mkdir(parents=True)
    readable = skills_directory(workspace)
    readable.parent.mkdir(parents=True)
    install_generation_skills(readable)

    presented = Path(env["CODEX_HOME"]) / "skills" / "example-skill" / "SKILL.md"
    assert presented.is_file(), "提示用が無いと、モデルはスキルの存在を知らない"
    assert (readable / "example-skill" / "SKILL.md").is_file(), "読み取り用が無いと資産をコピーできない"
    # 作業場所の中なので、許可は元から出ている。資格情報のある場所は含まない。
    profile = next(part for part in generation_permission_args(str(workspace))
                   if part.startswith("permissions."))
    assert json.dumps(str(workspace.resolve())) in profile
    assert str(home / "codex") not in profile


def test_the_skill_list_is_built_from_what_is_actually_installed(tmp_path, monkeypatch):
    """一覧を手で書かない。書くと、足した・外したときに必ずずれる。"""
    from backend.domain.generation import conventions, skill_catalogue, skills_note, skills_section

    source = tmp_path / "skills"
    for name, description in (("alpha-skill", "最初の説明。二文目は載せない。"),
                              ("beta-skill", "二つ目の説明。")):
        folder = source / name
        folder.mkdir(parents=True)
        (folder / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: {description}\n---\n本文\n", encoding="utf-8")
    # SKILL.md が無いディレクトリは数えない。
    (source / "not-a-skill").mkdir()

    # 説明はそのまま持つ。1文に切ると「いつ使うか」が落ちる。
    assert skill_catalogue(source) == [("alpha-skill", "最初の説明。二文目は載せない。"),
                                       ("beta-skill", "二つ目の説明。")]

    section = skills_section(source)
    assert "$APP_FORGE_SKILLS/alpha-skill/SKILL.md" in section
    assert "not-a-skill" not in section
    # 触ってはいけないことを、規約でも言う。作業場所の中なので書けてしまう。
    assert "編集・移動・削除しないこと" in section

    monkeypatch.setenv("GENERATION_SKILLS_ROOT", str(source))
    assert "alpha-skill" in conventions()
    assert "alpha-skill" not in conventions(with_skills=False)
    assert "alpha-skill" in skills_note()

    # スキルが無い構成では、あると書かない。
    empty = tmp_path / "none"
    empty.mkdir()
    assert skills_section(empty) == "" and skill_catalogue(empty) == []


def test_gemini_is_not_told_about_skills_it_cannot_reach(tmp_path):
    """Geminiはコマンドを実行できず、道具も作業場所の中しか見えない。

    スキルは作業場所の外にあるので、一覧を見せると「あると書いてあるのに
    読めない」になる。読める経路にだけ載せる。
    """
    async def run():
        for generator, expected in (("codex", True), ("gemini", False)):
            agent = Agent(AgentSettings(_env_file=None, user_id=uuid4(),
                                        token="test-only-" + "x" * 40,
                                        root=tmp_path / generator, generator=generator),
                          FakeBridge)
            payload = GenerationInput(job_id=uuid4(), project_id=uuid4(), specification=INPUT)
            workspace = agent.workspace_path(payload.project_id)
            workspace.mkdir(mode=0o700, parents=True, exist_ok=True)
            written = conventions(with_skills=expected)
            assert ("この作業場所にあるスキル" in written) is expected
            await agent.close()
    asyncio.run(run())


def test_the_skills_directory_never_becomes_part_of_the_application(tmp_path):
    """作業場所の中に置いたので、外さないと成果物にも履歴にも入る。

    基盤が配るものであってアプリのコードではない。ZIPに `.agents/` が入ると、
    受け取った人がアプリの一部だと思って持ち回すことになる。
    """
    from backend.core.codex_bridge import skills_directory
    from backend.core.code_history import EXCLUDED
    from backend.domain.generation import (code_bundle_from_workspace, is_generated_leftover)
    from backend.domain.workspace_tools import check_sources, list_sources

    workspace = tmp_path / "workspace"
    (workspace / "frontend" / "src").mkdir(parents=True)
    (workspace / "backend").mkdir()
    (workspace / "backend" / "main.py").write_text(
        "from fastapi import FastAPI\napp = FastAPI()\n@app.get('/')\ndef i(): return {}\n")
    (workspace / "pyproject.toml").write_text(PYPROJECT)
    (workspace / "frontend" / "package.json").write_text('{"scripts":{"build":"vite build"}}')
    (workspace / "frontend" / "src" / "app.vue").write_text("<template/>")

    skills = skills_directory(workspace)
    (skills / "fastapi-backend" / "assets").mkdir(parents=True)
    (skills / "fastapi-backend" / "SKILL.md").write_text("---\nname: x\n---\n")
    (skills / "fastapi-backend" / "assets" / "main.py").write_text("# 雛形\n")

    assert is_generated_leftover((".agents", "skills", "SKILL.md"))
    assert ".agents/" in EXCLUDED
    assert not any(".agents" in path for path in list_sources(workspace)["files"])
    # 受け取れない名前として報告もされない（黙って無いものとして扱う）。
    assert check_sources(workspace)["problems"] == []
    assert not any(".agents" in item.path for item in code_bundle_from_workspace(workspace).files)


def test_the_reason_a_turn_stopped_is_kept(tmp_path, caplog):
    """なぜ止まったかはCodexしか知らない。受け取っておいて捨てない。

    画面には決まった文面しか出せないので、手掛かりはログへ残す。捨てていたため、
    「[inference]」としか分からず、接続を疑うところから始めることになっていた。
    """
    import logging

    class Refusing(FakeBridge):
        turn_status = "failed"

    async def run():
        agent = Agent(settings(tmp_path), Refusing)
        payload = GenerationInput(job_id=uuid4(), project_id=uuid4(), specification=INPUT)
        with caplog.at_level(logging.WARNING, logger="uvicorn.error"):
            await agent.start_generation(payload)
            await agent.task
        assert agent.job_status(payload.job_id)["status"] == "failed"
        await agent.close()
    asyncio.run(run())
    reported = [r.getMessage() for r in caplog.records if "generation_failed" in r.getMessage()]
    assert reported, "止まった理由が記録されていない"
    assert '"stage"' in reported[0] and '"failure_code"' in reported[0]


def test_a_turn_that_runs_out_of_time_says_so(tmp_path):
    """時間切れを通信の中断と同じ文面にすると、接続を疑って押し直すことになる。

    段階を分けたぶん、実装ターンは検査まで走る。上限は Gemini 側と揃える。
    """
    from backend.domain.generation import GENERATION_ERRORS

    assert "timeout" in GENERATION_ERRORS
    assert GENERATION_ERRORS["timeout"] != GENERATION_ERRORS["inference"]
    assert "小さく頼み直して" in GENERATION_ERRORS["timeout"]

    class Stalling(FakeBridge):
        async def call(self, method, params=None):
            if method == "turn/start":
                await asyncio.sleep(5)
            return await super().call(method, params)

    async def run():
        agent = Agent(AgentSettings(_env_file=None, user_id=uuid4(),
                                    token="test-only-" + "x" * 40, root=tmp_path,
                                    turn_timeout=60), Stalling)
        # 既定は Gemini 側（1800）と揃える。短いと、進んでいるのに打ち切る。
        assert AgentSettings.model_fields["turn_timeout"].default == 1800
        await agent.close()
    asyncio.run(run())


def test_the_first_build_is_a_working_ledger_and_says_what_it_left_out():
    """初回から全部作らせない。

    利用者はコードを読めないので、出来上がるまで何も確かめられない。台帳が動く
    ところまでを先に出して、触ってもらってから足す。直前の生成は8ファイル更新の
    末に「ユーザー追加が管理画面に不足していた」と手を広げ、そのまま中断した。
    """
    from backend.domain.generation import generation_prompt
    from backend.domain.projects import ProjectInput

    text = generation_prompt(ProjectInput.model_validate(INPUT))
    assert "FIRST build" in text
    for deferred in ("No user management", "no roles", "no audit log"):
        assert deferred in text
    # 合言葉の照合だけは初回から。無いと素のままデータを返す。
    assert "X-Forge-Auth" in text
    # 見送ったものを申告させる。黙っていると「そういう仕様」と受け取られる。
    assert "NEXT:" in text

    # 直しの依頼は範囲を絞らない。少しずつ足していくのがこちらの経路。
    from backend.domain.generation import instruction_prompt
    assert "FIRST build" not in instruction_prompt("ユーザー管理を足して")


def test_the_application_never_builds_a_sign_in():
    """ログインはKoyorinaが済ませている。アプリは合言葉を照合するだけ。

    規約に「APP_FORWARD_SECRET が無ければGoogleログイン」という分岐が残っていた。
    生成アプリは常にKoyorinaの背後で動くので、その分岐は一度も使われない。
    """
    rules = conventions()
    assert "ログインを一切行いません" in rules
    assert "hmac.compare_digest" in rules and "X-Forge-Auth" in rules
    # 照合できないときに通さない。素通りさせる分岐を残さない。
    assert "`APP_FORWARD_SECRET` 自体が未設定" in rules
    assert "Google sign-in" not in rules and "招待の確認、セッションログインの" in rules


def test_deferred_items_reach_the_person_who_cannot_read_code(tmp_path):
    """見送ったものを黙っていると、「足りない」ではなく「そういう仕様」になる。"""
    from backend.domain import next_steps

    assert next_steps.from_model("NEXT: ユーザー管理を追加する\nNEXT ： 月次集計") == [
        "ユーザー管理を追加する", "月次集計"]
    assert next_steps.from_model("何も見送っていません。") == []
    # 無いのに枠だけ出さない。
    assert next_steps.summary([]) == ""
    assert "次はこのあたりを頼めます" in next_steps.summary(["ユーザー管理を追加する"])

    class Reporting(FakeBridge):
        final_text = ("## 作成しました\n\n- 一覧と登録画面\n\n```bash\nuv run pytest\n```\n"
                      "NEXT: ユーザー管理画面を追加する\nCOMMIT: feat: 台帳を追加")

    async def run():
        agent = Agent(settings(tmp_path), Reporting)
        payload = GenerationInput(job_id=uuid4(), project_id=uuid4(), specification=INPUT)
        await agent.start_generation(payload)
        await agent.task
        status = agent.job_status(payload.job_id)
        assert status["status"] == "generated" and status["next_steps"] == ["ユーザー管理画面を追加する"]
        # 最後の報告はチャットの返事になる。段落は残し、コードの塊と申告の行は抜く。
        assert status["summary"] == "## 作成しました\n\n- 一覧と登録画面\n\n（コードは省略しました）"
        await agent.close()
    asyncio.run(run())


def test_the_skill_list_keeps_the_part_that_says_when_to_use_it():
    """説明を1文に切ると、判断基準ごと落ちる。

    SKILL.md の description には「〜のときに必ず参照すること」と書いてある。
    そこが無いと、どれを読むかがモデルの当てずっぽうになる。
    """
    from backend.domain.generation import skill_catalogue, skills_section

    for name, summary in skill_catalogue():
        assert summary, name
    section = skills_section()
    assert "必ず参照すること" in section, "判断基準が載っていない"
    # どれが当てはまるかを、当てずっぽうにさせない。
    assert "必ず当てはまります" in section
    # 句点の二重付けをしない。
    assert "。。" not in section


def test_the_rules_are_true_in_the_turn_that_only_writes_the_manifests():
    """段階を分けたので、宣言ターンの時点ではまだ何も入っていない。

    「宣言した依存は導入済み」と書いたままだと、そのターンで vue-tsc や pytest を
    動かそうとして落ちる。順番として書く。
    """
    rules = conventions()
    assert "宣言だけを書くターンでは、まだ何も入っていません" in rules
    # 禁じたいのは取得であって、道具を使うこと自体ではない。
    assert ".venv/bin/pytest" in rules and "取得を伴わない使い方" in rules


def test_a_later_feature_can_bring_in_a_new_library(tmp_path, monkeypatch):
    """初回を絞った以上、機能追加で新しいライブラリが要ることは普通に起きる。

    導入は実装ターンの前に1回だけだったので、モデルが宣言へ足しても入れ直す機会が
    無く、import で落ちて修正ターンでも直せなかった（サンドボックスからは取得
    できない）。検査の前に毎回通す。宣言が同じなら飛ばすので、ふだんは何も起きない。
    """
    from backend.worker import toolchain_runner

    installs, verifies = [], []

    async def install(workspace, packages, report=None, cache=None):
        installs.append(dependency_digest(workspace))
        return []

    async def verify(workspace, report=None, cache=None):
        verifies.append(len(verifies))
        # 1回目の検査のあとにモデルが依存を足した、という状況を作る。
        if len(verifies) == 1:
            (workspace / "pyproject.toml").write_text(
                PYPROJECT.replace('"uvicorn[standard]>=0.34"',
                                  '"uvicorn[standard]>=0.34", "openpyxl>=3.1"'), encoding="utf-8")
            return ["$ pytest\n(終了コード 1)\nModuleNotFoundError: No module named 'openpyxl'"]
        return []

    monkeypatch.setattr(toolchain_runner, "install", install)
    monkeypatch.setattr(toolchain_runner, "verify", verify)

    async def run():
        agent = Agent(settings(tmp_path), FakeBridge)
        payload = GenerationInput(job_id=uuid4(), project_id=uuid4(), specification=INPUT,
                                  instruction="月次集計をExcelで出せるようにして")
        await agent.start_generation(payload)
        await agent.task
        assert agent.job_status(payload.job_id)["status"] == "generated"
        await agent.close()
    asyncio.run(run())
    # 宣言が変わったので、2回目の検査の前に入れ直している。
    assert len(installs) >= 2, "宣言を足しても導入し直していない"
    assert installs[-1] != installs[0], "変わった宣言で導入していない"


def test_the_rules_tell_the_model_it_may_add_a_dependency_later():
    """「あとから足せない」と書いたままだと、必要でも足さずに書こうとする。"""
    rules = conventions()
    assert "宣言に書き足せば入ります" in rules
    # 同じターンでは使えないことも言う。言わないと、自分で試して落ちて止まる。
    assert "そのターンの中では、まだ入っていません" in rules
    assert "ソースからのビルドが要るものは入らない" in rules


def test_databases_and_test_leftovers_never_fail_the_handover(tmp_path):
    """試験や実行が作るファイルで、生成ごと失敗させない。

    規約では成果物へ入れるなと書いているが、pytest を走らせれば
    backend/data/*.db も pytest-of-* も実際に出てくる。出たものを
    「受け取れない拡張子です」と言って失敗させても、モデルには直しようがない
    （消してもまた出る）。基盤が落とす。
    """
    from backend.domain.generation import code_bundle_from_workspace, is_generated_leftover
    from backend.domain.workspace_tools import check_sources, list_sources

    workspace = tmp_path / "workspace"
    (workspace / "frontend" / "src").mkdir(parents=True)
    (workspace / "backend" / "data").mkdir(parents=True)
    (workspace / "backend" / "main.py").write_text(
        "from fastapi import FastAPI\napp = FastAPI()\n@app.get('/')\ndef i(): return {}\n")
    (workspace / "pyproject.toml").write_text(PYPROJECT)
    (workspace / "frontend" / "package.json").write_text('{"scripts":{"build":"vite build"}}')
    (workspace / "frontend" / "src" / "app.vue").write_text("<template/>")
    # 実際に出たもの。
    (workspace / "backend" / "data" / "invoices.db").write_bytes(b"SQLite format 3\x00")
    leftover = workspace / "pytest-of-forge" / "pytest-0" / "test_models0"
    leftover.mkdir(parents=True)
    (leftover / "test.db").write_bytes(b"SQLite format 3\x00")
    (workspace / "pytest-of-forge" / "pytest-0" / ".lock").write_text("")
    synthetic = workspace / ".koyorina-tmp" / "codex-bwrap-synthetic-mount-targets-10001"
    synthetic.mkdir(parents=True)
    (synthetic / "lock").write_text("")

    for parts in (("backend", "data", "invoices.db"), ("pytest-of-forge", "pytest-0", ".lock"),
                  (".koyorina-tmp", "codex-bwrap-synthetic-mount-targets-10001", "lock"),
                  ("app.log",), ("backend", "x.sqlite3")):
        assert is_generated_leftover(parts), parts
    assert not is_generated_leftover(("backend", "main.py"))

    # 自己点検でも「直せ」と言わない。消してもまた出るものを指摘しない。
    assert check_sources(workspace)["problems"] == []
    assert not any(".koyorina-tmp" in item.path
                   for item in code_bundle_from_workspace(workspace).files)
    listed = list_sources(workspace)["files"]
    assert not [path for path in listed if path.endswith(".db") or "pytest-of-" in path]
    # 受け取りも通る。
    paths = [item.path for item in code_bundle_from_workspace(workspace).files]
    assert "backend/main.py" in paths
    assert not [path for path in paths if ".db" in path or "pytest-of-" in path]


def test_the_same_unfixable_failure_is_not_retried(tmp_path, monkeypatch):
    """1文字も変わらない失敗を投げ直しても、同じものが返るだけ。

    基盤側の不具合（既存 .venv で uv venv が落ちる）のとき、モデルは
    「Koyorina側で対処が必要」と正しく答えるが、こちらが何度も投げ直していた。
    """
    from backend.worker import toolchain_runner

    async def install(workspace, packages, report=None, cache=None):
        return ["$ uv venv .venv\n(終了コード 1)\nuv::venv::already exists"]

    monkeypatch.setattr(toolchain_runner, "install", install)
    monkeypatch.setattr(toolchain_runner, "verify", lambda *a, **k: _none())

    async def run():
        agent = Agent(settings(tmp_path), FakeBridge)
        payload = GenerationInput(job_id=uuid4(), project_id=uuid4(), specification=INPUT)
        await agent.start_generation(payload)
        await agent.task
        prompts = [p["input"][0]["text"] for m, p in agent.bridge.calls if m == "turn/start"]
        repairs = [t for t in prompts if "were run by Koyorina" in t]
        # 1回は試す。同じものが返ってきたら、そこで打ち切る。
        assert len(repairs) == 1, f"同じ失敗で {len(repairs)} 回投げ直している"
        await agent.close()
    asyncio.run(run())


def test_changing_the_model_starts_a_new_thread(tmp_path):
    """モデルは thread/start でしか渡せないかもしれない。

    スレッドはプロジェクト単位で使い回すので、2回目以降は thread/start を
    通らない。覚えておかないと、画面でモデルを変えても前のモデルのまま続く。
    """
    async def run():
        agent = Agent(settings(tmp_path), FakeBridge)
        project_id = uuid4()

        async def generate(model):
            payload = GenerationInput(job_id=uuid4(), project_id=project_id,
                                      specification=INPUT, model=model)
            await agent.start_generation(payload)
            await agent.task
            return [p for m, p in agent.bridge.calls if m == "thread/start"]

        await generate("gpt-5.6-sol")
        remembered = agent.thread_state(project_id)
        assert remembered["model"] == "gpt-5.6-sol"

        agent.bridge.calls.clear()
        # 同じモデルなら作り直さない。文脈を無駄に捨てない。
        assert await generate("gpt-5.6-sol") == []

        agent.bridge.calls.clear()
        # 変えたら作り直す。thread/start にも新しいモデルが乗る。
        starts = await generate("gpt-5.6-mini")
        assert len(starts) == 1 and starts[0]["model"] == "gpt-5.6-mini"
        assert agent.thread_state(project_id)["model"] == "gpt-5.6-mini"
        await agent.close()
    asyncio.run(run())


def test_the_model_codex_reports_is_preferred_over_the_one_we_asked_for():
    """要求した値をそのまま出すと、効いていなくても効いたように見える。"""
    from backend.worker.agent import reported_model

    assert reported_model({"id": "t1", "model": "gpt-5.6-sol"}) == "gpt-5.6-sol"
    # 版によって鍵の名前が変わりうるので広めに見る。入れ子でも拾う。
    assert reported_model({"usage": {}, "config": {"modelId": "gpt-5.6-mini"}}) == "gpt-5.6-mini"
    assert reported_model({"status": "completed"}) == ""
    assert reported_model(None) == ""


def test_a_side_menu_must_be_collapsible():
    """一覧表が主体の業務画面では、横幅がそのまま読める列数になる。

    240px のメニューが居座ると、その分だけ表が狭くなる。しかもメニューを見るのは
    画面を移るときだけで、作業中はずっと邪魔をしている。
    """
    rules = conventions()
    assert "必ず畳めるようにする" in rules
    # 閉じたあと戻せない作りにさせない。
    assert "開閉の操作はアプリバーに置き" in rules
    assert "畳んだ状態は記憶する" in rules

    skill = (Path(__file__).resolve().parents[2]
             / "Skills/vue-vuetify-frontend/SKILL.md").read_text(encoding="utf-8")
    assert "サイドメニューは畳めるようにする" in skill
    assert "v-navigation-drawer" in skill and "v-app-bar-nav-icon" in skill
    # 狭い画面で画面の半分を占めさせない。
    assert "useDisplay" in skill and "temporary" in skill


def test_verification_is_skipped_rather_than_blamed_on_the_application(tmp_path, monkeypatch):
    """閉じ込めが使えないPodで、モデルに修正を頼まない。"""
    from backend.worker import toolchain_runner

    monkeypatch.setattr(toolchain_runner, "install", lambda *a, **k: _none())
    monkeypatch.setattr(toolchain_runner, "verify",
                        lambda *a, **k: pytest.fail("検査を走らせてはいけない"))

    async def blocked(workspace, cache=None):
        return "この生成Podでは検査を実行できません（閉じ込めの仕組みを起動できない）。"

    monkeypatch.setattr(toolchain_runner, "sandbox_problem", blocked)

    async def run():
        agent = Agent(settings(tmp_path), FakeBridge)
        payload = GenerationInput(job_id=uuid4(), project_id=uuid4(), specification=INPUT)
        await agent.start_generation(payload)
        await agent.task
        # 生成は完了させる。検査できないことと、書けていないことは違う。
        assert agent.job_status(payload.job_id)["status"] == "generated"
        prompts = [p["input"][0]["text"] for m, p in agent.bridge.calls if m == "turn/start"]
        assert not [t for t in prompts if "were run by Koyorina" in t], "直せない相手に頼んでいる"
        events = (agent.job_path(payload.job_id) / "progress.json").read_text()
        assert "検査を実行できません" in events
        await agent.close()
    asyncio.run(run())


def test_antigravity_prompt_asks_for_the_whole_application_not_only_manifests():
    from uuid import uuid4
    from backend.worker.agent import GenerationInput, antigravity_prompt
    spec = ProjectInput(**INPUT)
    first = antigravity_prompt(GenerationInput(job_id=uuid4(), project_id=uuid4(), specification=spec))
    # 宣言ターン用の文面を渡すと、アプリのコードを書かずに終わる。
    assert "write ONLY the two dependency manifests" not in first
    assert "Build the application" in first
    assert "frontend/src/App.vue" in first and ".agents/AGENTS.md" in first

    change = antigravity_prompt(GenerationInput(job_id=uuid4(), project_id=uuid4(), specification=spec,
                                                instruction="一覧に検索欄を付けてください。"),
                                notes="\n添付資料")
    assert "write ONLY the two dependency manifests" not in change
    assert "一覧に検索欄を付けてください。" in change and change.endswith("\n添付資料")


@pytest.mark.parametrize("route", [
    '@app.get("/")',
    '@app.get("/{path:path}")',
    '@app.get("/{full_path:path}")',
    '@app.get("/{rest_of_path:path}", include_in_schema=False)',
    '@app.api_route("/{full_path:path}", methods=["GET", "HEAD"])',
    '@app.api_route("/{full_path:path}")',
])
def test_spa_entry_accepts_any_path_variable_name(route):
    from backend.domain.generation import runtime_contract_problems
    main = f"from fastapi import FastAPI\napp = FastAPI()\n{route}\nasync def spa(full_path: str = ''):\n    return {{}}\n"
    assert not any("画面の入口" in problem for problem in runtime_contract_problems({"backend/main.py": main}))


@pytest.mark.parametrize("route", [
    '@app.get("/api/items")',
    '@app.post("/{full_path:path}")',
    '@app.api_route("/{full_path:path}", methods=["POST"])',
    '@app.get("/{item_id}")',
])
def test_spa_entry_is_still_required(route):
    from backend.domain.generation import runtime_contract_problems
    main = f"from fastapi import FastAPI\napp = FastAPI()\n{route}\nasync def handler():\n    return {{}}\n"
    assert any("画面の入口" in problem for problem in runtime_contract_problems({"backend/main.py": main}))


def _revalidation_setup(tmp_path, files, *, failure_code="validation_contract"):
    from backend.domain.generation_progress import Progress
    agent = Agent(settings(tmp_path), FakeBridge)
    job_id = uuid4()
    workspace = agent.workspace_path(PROJECT_ID)
    for item in files:
        target = workspace / item["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(item["content"], encoding="utf-8")
    agent.write_status(job_id, "failed", "検査で止まりました。", failure_code=failure_code,
                       usage={"total_tokens": 42}, generator="antigravity")
    Progress(agent.job_path(job_id), str(job_id)).record("status", "最初の生成の記録")
    return agent, job_id


def _revalidate(agent, job_id):
    from backend.worker.agent import RevalidateInput
    return asyncio.run(agent.revalidate(job_id, RevalidateInput(
        project_id=PROJECT_ID, specification=INPUT, requested_by="検査担当")))


def test_revalidation_completes_a_job_without_regenerating(tmp_path):
    from backend.domain.generation_progress import Progress
    agent, job_id = _revalidation_setup(tmp_path, BUNDLE["files"])
    result = _revalidate(agent, job_id)
    assert result["status"] == "generated" and result["failure_code"] is None
    # 使った枠と生成元は、元の生成のものを引き継ぐ。
    assert result["usage"] == {"total_tokens": 42} and result["generator"] == "antigravity"
    assert (agent.job_path(job_id) / "bundle.json").is_file()
    messages = [event["message"] for event in Progress.read(agent.job_path(job_id))["events"]]
    assert messages[0] == "最初の生成の記録"  # それまでの経過を消さない
    assert any("生成を完了にしました" in message for message in messages)
    assert agent.active_job is None and agent.generation_lock is None


def test_revalidation_keeps_the_job_failed_when_the_check_still_fails(tmp_path):
    agent, job_id = _revalidation_setup(tmp_path, NO_ENTRY["files"])
    result = _revalidate(agent, job_id)
    assert result["status"] == "failed" and result["failure_code"] == "validation_contract"
    # 何が足りないかを、ファイル単位で返す。
    assert any(problem.startswith("backend/main.py:") and "画面の入口" in problem
               for problem in result["problems"])
    assert not (agent.job_path(job_id) / "bundle.json").exists()
    assert agent.active_job is None and agent.generation_lock is None


@pytest.mark.parametrize("failure_code", ["cancelled", "timeout", "validation_entrypoints"])
def test_any_failed_job_can_be_revalidated(tmp_path, failure_code):
    """検査の後に再生成を止めると、最新のジョブは「停止」になる。それでも完了にできる。"""
    agent, job_id = _revalidation_setup(tmp_path, BUNDLE["files"], failure_code=failure_code)
    assert _revalidate(agent, job_id)["status"] == "generated"


def test_a_finished_job_is_not_revalidated(tmp_path):
    agent, job_id = _revalidation_setup(tmp_path, BUNDLE["files"])
    agent.write_status(job_id, "generated", generator="antigravity")
    with pytest.raises(HTTPException) as error:
        _revalidate(agent, job_id)
    assert error.value.status_code == 409


def test_check_report_lists_every_problem_on_its_own_line():
    from backend.worker.agent import check_report
    problems = [f"frontend/src/file{index}.vue: 問題{index}" for index in range(25)]
    report = check_report(problems)
    assert report.splitlines()[0] == "受け取り検査で見つかった問題（25件）"
    assert "・frontend/src/file19.vue: 問題19" in report.splitlines()
    assert report.splitlines()[-1] == "（ほか 5 件）"


@pytest.mark.parametrize("code", [
    'app.add_api_route("/{full_path:path}", spa, methods=["GET"])',
    'app.add_api_route("/", spa)',
    'app.mount(path="/", app=StaticFiles(directory="dist", html=True))',
    '@app.exception_handler(404)\nasync def spa(request, exc):\n    return {}',
])
def test_other_valid_screen_entries_are_accepted(code):
    from backend.domain.generation import runtime_contract_problems
    main = f"from fastapi import FastAPI\napp = FastAPI()\nasync def spa(): return {{}}\n{code}\n"
    assert not any("画面の入口" in p for p in runtime_contract_problems({"backend/main.py": main}))


@pytest.mark.parametrize("scripts, ok", [
    # Vue 公式の雛形（create-vue）の形。
    ({"build": "run-p type-check \"build-only {@}\" --", "build-only": "vite build",
      "type-check": "vue-tsc --build"}, True),
    ({"build": "npm run typecheck && npm run bundle", "typecheck": "vue-tsc --noEmit",
      "bundle": "vite build --emptyOutDir"}, True),
    ({"build": "vue-tsc --noEmit&&vite build"}, True),
    ({"build": "vite"}, False),
    ({"build": "run-p build-only", "build-only": "vite"}, False),
    ({"build": "npm run build"}, False),  # 自分自身を呼ぶだけ
])
def test_build_script_may_call_vite_through_other_scripts(scripts, ok):
    import json
    from backend.domain.generation import runtime_contract_problems
    sources = {"frontend/package.json": json.dumps({"scripts": scripts}), "frontend/tsconfig.json": "{}"}
    problems = runtime_contract_problems(sources)
    assert (not any("vite build" in p for p in problems)) is ok


def test_vue_tsc_reached_through_another_script_still_needs_tsconfig():
    import json
    from backend.domain.generation import runtime_contract_problems
    package = {"scripts": {"build": "run-p type-check build-only", "build-only": "vite build",
                           "type-check": "vue-tsc --build"}}
    problems = runtime_contract_problems({"frontend/package.json": json.dumps(package)})
    assert any(p.startswith("frontend/tsconfig.json") for p in problems)


@pytest.mark.parametrize("section", ["dependencies", "devDependencies"])
@pytest.mark.parametrize("name, version", [
    ("vite", "^5.4.0"), ("vite", "^8.1.0"), ("vite", "^8.2.0"),
    ("@vitejs/plugin-vue", "^5.0.0"), ("@vitejs/plugin-vue", "^6.0.8"),
])
def test_generation_lets_the_build_check_dependency_compatibility(section, name, version):
    import json
    from backend.domain.generation import runtime_contract_problems
    package = {"scripts": {"build": "vite build"}, section: {name: version}}
    problems = runtime_contract_problems({"frontend/package.json": json.dumps(package)})
    assert problems == []


def test_generation_template_uses_a_buildable_starting_point():
    import json
    from backend.domain import generation
    root = Path(generation.__file__).resolve().parents[2]
    source = (root / "Skills/vue-vuetify-frontend/assets/package.json").read_text()
    package = json.loads(source)
    assert 'vite' in package['devDependencies']
    assert '@vitejs/plugin-vue' in package['devDependencies']
    assert generation.runtime_contract_problems({
        "frontend/package.json": source, "frontend/tsconfig.json": "{}"}) == []


def test_the_guide_states_every_acceptance_rule():
    """検査で落とす条件は、生成AIに渡す規約へ全部書いておく。

    知らされていない条件は守りようがなく、動くアプリでも失敗し続ける。
    検査の定数を変えたら、このテストが規約の書き直しを求める。
    """
    from backend.domain import generation
    from backend.domain.workspace_tools import MAX_FILE_BYTES
    guide = (Path(generation.__file__).resolve().parents[1] / "conventions" / "AGENTS.md").read_text()
    for suffix in generation.ALLOWED_SUFFIXES:
        assert f"`{suffix}`" in guide, suffix
    for name in generation.DENIED_FILENAMES | generation.ALLOWED_DOTFILES:
        assert f"`{name}`" in guide, name
    for required in ("backend/main.py", "pyproject.toml", "frontend/package.json", "frontend/src/App.vue"):
        assert f"`{required}`" in guide, required
    assert f"{MAX_FILE_BYTES:,} バイト" in guide
    assert "100件まで" in guide and "5MB" in guide and "200 まで" in guide
    for package in generation.BASELINE_PACKAGES:
        assert f"`{package}`" in guide, package
    assert "`npm run build`" in guide
    assert "{full_path:path}" in guide and "vite build" in guide


def test_saved_bundle_validation_reports_code_problem_without_source(tmp_path):
    import copy
    cfg = settings(tmp_path)
    app = create_agent(cfg)
    job_id = uuid4()
    agent = app.state.agent
    folder = agent.job_path(job_id)
    folder.mkdir(parents=True)
    agent.write_status(job_id, 'generated')
    raw = copy.deepcopy(BUNDLE)
    package = next(f for f in raw['files'] if f['path'] == 'frontend/package.json')
    manifest = json.loads(package['content'])
    manifest.setdefault('scripts', {})['build'] = 'vite'
    package['content'] = json.dumps(manifest)
    (folder / 'bundle.json').write_text(json.dumps(raw))
    with TestClient(app) as client:
        result = client.get(f'/jobs/{job_id}/bundle', headers={'Authorization': 'Bearer ' + cfg.token.get_secret_value()})
    assert result.status_code == 409
    assert 'buildスクリプトでvite build' in result.text
    assert 'AIの接続状態' not in result.text
    assert 'files' not in result.text


def test_saved_bundle_with_different_vite_version_can_be_built(tmp_path):
    import copy
    cfg = settings(tmp_path)
    app = create_agent(cfg)
    job_id = uuid4()
    agent = app.state.agent
    folder = agent.job_path(job_id)
    folder.mkdir(parents=True)
    agent.write_status(job_id, 'generated')
    raw = copy.deepcopy(BUNDLE)
    package = next(f for f in raw['files'] if f['path'] == 'frontend/package.json')
    manifest = json.loads(package['content'])
    manifest.setdefault('devDependencies', {})['vite'] = '^7.0.0'
    package['content'] = json.dumps(manifest)
    (folder / 'bundle.json').write_text(json.dumps(raw))
    with TestClient(app) as client:
        result = client.get(f'/jobs/{job_id}/bundle',
            headers={'Authorization': 'Bearer ' + cfg.token.get_secret_value()})
    assert result.status_code == 200
    assert any(file['path'] == 'frontend/package.json' for file in result.json()['files'])


def test_controller_relays_bundle_validation_reason(monkeypatch):
    import base64
    import httpx
    import backend.worker.controller as controller_module
    from backend.worker.controller import Provisioner
    provisioner = Provisioner(ControllerSettings(token='test-only-' + 'x' * 40,
        agent_image='registry.example.com/koyorina-agent@sha256:' + 'a' * 64))
    async def kube(method, resource, name='', body=None):
        if resource == 'pods':
            return {'status': {'podIP': '10.0.0.2', 'conditions': [{'type': 'Ready', 'status': 'True'}]}}
        return {'data': {'token': base64.b64encode(b'worker-token').decode()}}
    provisioner.kube = kube
    message = 'この生成版は現在のビルド条件に適合しません。viteを修正してください。'
    original = httpx.AsyncClient
    transport = httpx.MockTransport(lambda request: httpx.Response(409, json={'detail': message}))
    monkeypatch.setattr(controller_module.httpx, 'AsyncClient', lambda **kwargs: original(transport=transport, **kwargs))
    async def check():
        with pytest.raises(HTTPException) as failure:
            await provisioner.relay(uuid4(), 'GET', f'/jobs/{uuid4()}/bundle', tenant=uuid4())
        assert failure.value.status_code == 409 and failure.value.detail == message
    asyncio.run(check())


def test_codex_model_list_is_read_in_the_app_server_shape():
    """app-server の model/list はキャメルケースで返す。表示名・既定・推論の段階を取りこぼさない。"""
    from backend.domain.generation import model_options
    raw = {"data": [
        {"id": "gpt-6.1-sol", "displayName": "GPT-6.1-Sol", "isDefault": True, "hidden": False,
         "defaultReasoningEffort": "low", "description": "default",
         "supportedReasoningEfforts": [{"reasoningEffort": "low"}, {"reasoningEffort": "high"}]},
        {"id": "gpt-6-luna", "displayName": "GPT-6-Luna", "isDefault": False,
         "defaultReasoningEffort": "medium", "supportedReasoningEfforts": [{"reasoningEffort": "high"}]},
        {"id": "hidden-model", "hidden": True},
        # 以前の形（スネークケース）も読める。
        {"id": "gpt-old", "display_name": "Old", "is_default": False, "default_reasoning_effort": "high",
         "supported_reasoning_efforts": [{"reasoning_effort": "high"}]},
    ], "nextCursor": None}
    options = {option["id"]: option for option in model_options(raw)}
    assert list(options) == ["gpt-6.1-sol", "gpt-6-luna", "gpt-old"]
    assert options["gpt-6.1-sol"]["label"] == "GPT-6.1-Sol" and options["gpt-6.1-sol"]["is_default"]
    assert options["gpt-6.1-sol"]["efforts"] == ["low", "high"]
    assert options["gpt-6.1-sol"]["default_effort"] == "low"
    # 既定の段階が選べない場合は、選べる最初の段階にする。
    assert options["gpt-6-luna"]["default_effort"] == "high" and not options["gpt-6-luna"]["is_default"]
    assert options["gpt-old"]["label"] == "Old" and options["gpt-old"]["efforts"] == ["high"]


def test_model_list_reports_preparing_instead_of_failing_while_the_pod_starts():
    """起動中にモデル一覧を求められても、失敗にしない。画面がCodexを黙って外してしまう。"""
    import asyncio
    from backend.worker.controller import Provisioner
    settings = ControllerSettings(token="test-only-" + "x" * 40,
        agent_image="registry.example.com/koyorina-agent@sha256:" + "a" * 64)
    provisioner = Provisioner(settings)
    existing, pods = {"persistentvolumeclaims"}, {}

    async def kube(method, resource, name="", body=None):
        if method == "POST":
            existing.add(resource)
            return {"metadata": {"name": name}}
        if resource == "pods" and pods:
            return pods
        return {"metadata": {"name": name}} if resource in existing else None

    provisioner.kube = kube
    # Podが無い（片付けられた）→ 用意を始めて、準備中と返す。
    assert asyncio.run(provisioner.relay(uuid4(), "GET", "/models")) == {"models": [], "status": "preparing"}
    # Podはあるが、まだ準備ができていない。
    pods.update({"metadata": {"name": "p"}, "status": {"phase": "Pending"}})
    assert asyncio.run(provisioner.relay(uuid4(), "GET", "/models")) == {"models": [], "status": "preparing"}
    # 使ったことが無い人は未接続。待たせない。
    existing.clear()
    pods.clear()
    assert asyncio.run(provisioner.relay(uuid4(), "GET", "/models")) == {"models": [], "status": "disconnected"}
