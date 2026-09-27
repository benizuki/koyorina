"""生成モデルへ渡す能力の境界。ここが崩れるとワークスペース外へ手が届く。"""
import pytest
from backend.domain.workspace_tools import collect_sources, list_sources, read_source, write_source


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "workspace"
    (root / "backend").mkdir(parents=True)
    (root / "backend" / "main.py").write_text("x = 1\n")
    (tmp_path / "secret.txt").write_text("外部の秘密")
    return root


@pytest.mark.parametrize("path", [
    "../secret.txt", "../../etc/passwd", "/etc/passwd", "backend/../../secret.txt",
    "backend/main.py/../../../secret.txt", ".env", ".ssh/id_rsa", "backend/main.exe",
])
def test_paths_outside_the_workspace_are_refused(workspace, path):
    assert write_source(workspace, path, "x")["status"] == "rejected"
    assert read_source(workspace, path)["status"] == "rejected"
    assert not (workspace.parent / "secret.txt").read_text() == "x"


def test_symlinks_are_refused(workspace):
    (workspace / "backend" / "link.py").symlink_to(workspace.parent / "secret.txt")
    assert read_source(workspace, "backend/link.py")["status"] == "rejected"
    assert write_source(workspace, "backend/link.py", "x")["status"] == "rejected"
    assert (workspace.parent / "secret.txt").read_text() == "外部の秘密"


def test_write_read_and_list(workspace):
    assert write_source(workspace, "frontend/src/App.vue", "<template/>")["status"] == "written"
    assert read_source(workspace, "frontend/src/App.vue")["text"] == "<template/>"
    assert read_source(workspace, "backend/missing.py")["status"] == "missing"
    listed = list_sources(workspace)
    assert listed["files"] == ["backend/main.py", "frontend/src/App.vue"]


def test_oversized_and_generated_output_are_excluded(workspace):
    assert write_source(workspace, "backend/big.py", "x" * 200_001)["status"] == "rejected"
    (workspace / "frontend" / "node_modules" / "pkg").mkdir(parents=True)
    (workspace / "frontend" / "node_modules" / "pkg" / "index.js").write_text("//")
    (workspace / "AGENTS.md").write_text("規約")
    # 依存物と規約は成果物ではない。一覧にも出さない。
    assert list_sources(workspace)["files"] == ["backend/main.py"]


def test_the_download_holds_what_the_file_tab_shows(workspace):
    (workspace / "frontend" / "node_modules").mkdir(parents=True)
    (workspace / "frontend" / "node_modules" / "index.js").write_text("//")
    (workspace / "AGENTS.md").write_text("規約")
    (workspace / ".env").write_text("SECRET=1")
    (workspace / "backend" / "link.py").symlink_to(workspace.parent / "secret.txt")
    collected = collect_sources(workspace)
    assert collected["files"] == [{"path": "backend/main.py", "content": "x = 1\n"}]
    assert collected["truncated"] is False
    # シンボリックリンクは一覧に名前だけ出るが、開けないのでZIPにも入れない。
    assert list_sources(workspace)["files"] == ["backend/link.py", "backend/main.py"]


def test_check_sources_reports_syntax_errors_and_missing_required_files(workspace):
    """自分でコマンドを流せないエージェントのために、受け取れない理由を先に返す。"""
    from backend.domain.workspace_tools import check_sources
    write_source(workspace, "backend/main.py", "def broken(:\n    pass\n")
    write_source(workspace, "frontend/package.json", "{invalid}")
    problems = check_sources(workspace)["problems"]
    assert any(p.startswith("backend/main.py:1:") and "構文" in p for p in problems)
    assert any(p.startswith("frontend/package.json:1:") for p in problems)
    assert any("frontend/src/app.vue: 必須ファイル" in p for p in problems)

    write_source(workspace, "backend/main.py", "app = 1\n")
    write_source(workspace, "frontend/package.json", '{"name":"app","scripts":{"build":"vite build"}}')
    write_source(workspace, "frontend/src/app.vue", "<template><div /></template>")
    write_source(workspace, "pyproject.toml", '[project]\nname = "ledger"\nversion = "0.1.0"\nrequires-python = ">=3.14"\ndependencies = ["fastapi>=0.141", "uvicorn[standard]>=0.34"]\n')
    assert check_sources(workspace) == {"status": "ok", "problems": []}


def test_tsconfig_may_carry_comments_but_package_json_may_not(workspace):
    """tsconfigはコメント付きで配られるのが普通。厳密なJSONで弾くと正しい生成物を落とす。"""
    from backend.domain.generation import parse_json_source
    from backend.domain.workspace_tools import check_sources
    tsconfig = '{\n  /* Bundler mode */\n  "moduleResolution": "bundler", // 既定\n  "strict": true,\n}\n'
    write_source(workspace, "frontend/tsconfig.json", tsconfig)
    write_source(workspace, "backend/main.py", "app = 1\n")
    write_source(workspace, "frontend/package.json", '{"name":"app","scripts":{"build":"vite build"}}')
    write_source(workspace, "frontend/src/app.vue", "<template><div /></template>")
    write_source(workspace, "pyproject.toml", '[project]\nname = "ledger"\nversion = "0.1.0"\nrequires-python = ">=3.14"\ndependencies = ["fastapi>=0.141", "uvicorn[standard]>=0.34"]\n')
    assert check_sources(workspace)["problems"] == []

    # 文字列の中は触らない。URLの // を消すと、通るはずのものが壊れる。
    parse_json_source("frontend/tsconfig.json", '{"url": "https://example.com/a/*b*/"}')
    with pytest.raises(ValueError):
        parse_json_source("frontend/package.json", '{"name": "app", // 説明\n}')


def test_check_sources_requires_frontend_build_and_non_404_root(workspace):
    from backend.domain.workspace_tools import check_sources
    write_source(workspace, "backend/main.py", "from fastapi import FastAPI\napp = FastAPI()\n")
    write_source(workspace, "frontend/package.json", '{"name":"app"}')
    write_source(workspace, "frontend/src/app.vue", "<template><div /></template>")
    write_source(workspace, "pyproject.toml", '[project]\nname = "ledger"\nversion = "0.1.0"\nrequires-python = ">=3.14"\ndependencies = ["fastapi>=0.141", "uvicorn[standard]>=0.34"]\n')
    problems = check_sources(workspace)["problems"]
    assert any("vite build" in problem for problem in problems)
    assert any("GET /" in problem for problem in problems)

    write_source(workspace, "backend/main.py",
                 "from fastapi import FastAPI\napp = FastAPI()\n@app.get('/')\ndef index(): return 'ok'\n")
    write_source(workspace, "frontend/package.json", '{"scripts":{"build":"vue-tsc --noEmit && vite build"}}')
    write_source(workspace, "frontend/tsconfig.json", '{"compilerOptions":{"strict":true}}')
    assert check_sources(workspace) == {"status": "ok", "problems": []}


def test_gemini_also_gets_the_checks_run_for_it(tmp_path, monkeypatch):
    """Geminiはコマンドを実行できない。だからこそ、検査は基盤が走らせる。

    Codexと段階の分け方は違うが、「基盤が確かめて、落ちた内容を戻す」ところは同じ。
    モデルの「通りました」で先へ進めない。
    """
    import asyncio
    from uuid import uuid4
    from backend.worker import toolchain_runner
    from backend.worker.agent import Agent, AgentSettings, GenerationInput
    from backend.tests.test_projects import INPUT

    rounds = []

    async def verify(workspace, report=None, cache=None):
        rounds.append(1)
        return ["$ vite build\n(終了コード 1)\nsrc/app.vue(9,1): error TS1005"] if len(rounds) == 1 else []

    async def available(*args, **kwargs):
        return ""

    monkeypatch.setattr(toolchain_runner, "verify", verify)
    monkeypatch.setattr(toolchain_runner, "install", lambda *a, **k: _empty())
    monkeypatch.setattr(toolchain_runner, "sandbox_problem", available)

    prompts = []

    async def fake_turn(workspace, model, specification, instruction, progress, **kwargs):
        prompts.append(kwargs.get("text"))
        return {"total_tokens": 5}

    monkeypatch.setattr("backend.worker.agent.gemini_turn", fake_turn)

    async def run():
        agent = Agent(AgentSettings(_env_file=None, user_id=uuid4(),
                                    token="test-only-" + "x" * 40, root=tmp_path,
                                    generator="gemini"))
        payload = GenerationInput(job_id=uuid4(), project_id=uuid4(), specification=INPUT)
        workspace = agent.workspace_path(payload.project_id)
        workspace.mkdir(mode=0o700, parents=True, exist_ok=True)
        # 受け取り検査も修正ループで見る。空の作業場所だとそちらで毎回落ちるので、
        # 受け取れる最小のアプリを置き、ビルドの失敗だけを確かめる。
        from backend.tests.test_codex_generation import BUNDLE
        for item in BUNDLE["files"]:
            (workspace / item["path"]).parent.mkdir(parents=True, exist_ok=True)
            (workspace / item["path"]).write_text(item["content"], encoding="utf-8")
        usage = await agent.gemini_phased(payload, workspace, _Progress())
        # 生成1回 + 修正1回ぶんを合算して返す。
        assert usage == {"total_tokens": 10}
        assert prompts[0] is None
        assert "src/app.vue(9,1): error TS1005" in prompts[1]
        await agent.close()
    asyncio.run(run())
    assert len(rounds) == 2


async def _empty():
    return []


class _Progress:
    def record(self, *args, **kwargs):
        pass


def test_gemini_is_not_told_about_skills_in_its_system_instruction():
    """Geminiが実際に読むのはシステム指示。作業場所のAGENTS.mdではない。

    置くファイルのほうだけ経路で出し分けても、こちらに一覧が載っていれば
    「あると書いてあるのに読めない」は解消しない。
    """
    from pathlib import Path as FilePath
    source = (FilePath(__file__).resolve().parents[1]
              / "worker" / "antigravity_sdk_agent.py").read_text(encoding="utf-8")
    assert "conventions(with_skills=False)" in source
    assert "system_instruction=conventions()" not in source


def test_the_repair_loop_also_returns_acceptance_problems(tmp_path, monkeypatch):
    """ビルドが通っても、受け取れないものは修正ターンへ戻す。最後の検査で初めて落とさない。"""
    import asyncio
    from uuid import uuid4
    from backend.worker import toolchain_runner
    from backend.worker.agent import Agent, AgentSettings, GenerationInput
    from backend.tests.test_projects import INPUT
    from backend.tests.test_codex_generation import NO_ENTRY

    async def passes(*args, **kwargs):
        return []

    async def available(*args, **kwargs):
        return ""

    monkeypatch.setattr(toolchain_runner, "verify", passes)
    monkeypatch.setattr(toolchain_runner, "install", lambda *a, **k: _empty())
    monkeypatch.setattr(toolchain_runner, "sandbox_problem", available)
    requests = []

    async def repair(text):
        requests.append(text)
        main = workspace / "backend/main.py"
        main.write_text(main.read_text() + "@app.get('/')\ndef index(): return {}\n")
        return {"total_tokens": 1}

    agent = Agent(AgentSettings(_env_file=None, user_id=uuid4(),
                                token="test-only-" + "x" * 40, root=tmp_path))
    payload = GenerationInput(job_id=uuid4(), project_id=uuid4(), specification=INPUT)
    workspace = agent.workspace_path(payload.project_id)
    for item in NO_ENTRY["files"]:
        (workspace / item["path"]).parent.mkdir(parents=True, exist_ok=True)
        (workspace / item["path"]).write_text(item["content"], encoding="utf-8")
    asyncio.run(agent.verify_and_repair(payload, workspace, _Progress(), repair=repair))
    assert len(requests) == 1 and "画面の入口" in requests[0] and "backend/main.py" in requests[0]
