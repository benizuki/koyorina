"""生成コードの変更履歴。生成1回ごとに、何が変わったかを残す。

「動作確認へ出した記録」とは別物。あちらは区切り（リリース）で、こちらは変更そのもの。
プレビューへ出していない生成もここに残る。

置き場を作業場所の外にしているのが肝。中に置くと、そこは生成モデルが書ける領域なので、
履歴そのものを書き換えたり消したりできてしまう。
"""
import asyncio
import shutil
from types import SimpleNamespace
from pathlib import Path
from uuid import uuid4
import pytest
from backend.core import code_history as history

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="gitが無い環境")


def workspace_with(root: Path, project_id, files: dict) -> Path:
    workspace = root / "projects" / str(project_id)
    for name, text in files.items():
        target = workspace / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    return workspace


def test_each_generation_is_recorded_with_who_asked(tmp_path):
    async def run():
        project = uuid4()
        workspace = workspace_with(tmp_path, project, {"backend/main.py": "app = 1\n"})
        first = await history.record(tmp_path, project, workspace, "初回生成",
                                     "太郎 <t@example.test>")
        assert first
        workspace_with(tmp_path, project, {"backend/main.py": "app = 2\n"})
        await history.record(tmp_path, project, workspace, "ステータスを追加",
                             "花子 <h@example.test>")
        rows = await history.entries(tmp_path, project, workspace)
        assert [(row["author"], row["summary"]) for row in rows] == [
            ("花子", "ステータスを追加"), ("太郎", "初回生成")]
    asyncio.run(run())


def test_the_repository_itself_is_outside_the_model_writable_workspace(tmp_path):
    """作業場所にあるのは指し示すファイルだけ。実体は書き込み権限の外に置く。

    実体を中に置くと、生成モデルが `git reset --hard` や削除で履歴を壊せる。
    指し示すだけなら、壊されても次の記録で作り直せる。
    """
    async def run():
        project = uuid4()
        workspace = workspace_with(tmp_path, project, {"backend/main.py": "app = 1\n"})
        await history.record(tmp_path, project, workspace, "初回生成")
        repo = history.repository(tmp_path, project)
        pointer = workspace / ".git"
        assert pointer.is_file() and pointer.read_text().strip() == f"gitdir: {repo}"
        assert repo.is_dir() and not repo.resolve().is_relative_to(workspace.resolve())
    asyncio.run(run())


def test_the_model_can_read_the_history_from_the_workspace(tmp_path):
    """不具合を追うとき、生成モデル自身が差分を見られるほうが早い。"""
    async def run():
        project = uuid4()
        workspace = workspace_with(tmp_path, project, {"backend/main.py": "app = 1\n"})
        await history.record(tmp_path, project, workspace, "初回生成")
        # 作業場所で素の git が通る（--git-dir を指定しない）。
        process = await asyncio.create_subprocess_exec(
            "git", "log", "--oneline", cwd=workspace,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env={"PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": str(tmp_path),
                 "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"})
        out, _ = await process.communicate()
        assert process.returncode == 0 and "初回生成" in out.decode()
    asyncio.run(run())


def test_a_deleted_pointer_does_not_lose_the_history(tmp_path):
    """モデルが .git を消しても、実体は権限の外にあるので残る。"""
    async def run():
        project = uuid4()
        workspace = workspace_with(tmp_path, project, {"backend/main.py": "app = 1\n"})
        await history.record(tmp_path, project, workspace, "初回生成")
        (workspace / ".git").unlink()
        workspace_with(tmp_path, project, {"backend/main.py": "app = 2\n"})
        assert await history.record(tmp_path, project, workspace, "続き")
        assert len(await history.entries(tmp_path, project, workspace)) == 2
        assert (workspace / ".git").is_file()
    asyncio.run(run())


def test_nothing_is_recorded_when_nothing_changed(tmp_path):
    """同じ内容の記録が並ぶと、履歴が読めなくなる。"""
    async def run():
        project = uuid4()
        workspace = workspace_with(tmp_path, project, {"backend/main.py": "app = 1\n"})
        assert await history.record(tmp_path, project, workspace, "初回生成")
        assert await history.record(tmp_path, project, workspace, "変化なし") == ""
        assert len(await history.entries(tmp_path, project, workspace)) == 1
    asyncio.run(run())


def test_generated_leftovers_are_not_recorded(tmp_path):
    """node_modules などを記録すると、履歴が膨らんで差分も読めなくなる。"""
    async def run():
        project = uuid4()
        workspace = workspace_with(tmp_path, project, {
            "backend/main.py": "app = 1\n", "node_modules/left.js": "junk",
            "__pycache__/x.pyc": "junk", "attachments/note.pdf": "junk"})
        commit = await history.record(tmp_path, project, workspace, "初回生成")
        diff = await history.difference(tmp_path, project, workspace, commit)
        assert [item["path"] for item in diff["files"]] == ["backend/main.py"]
    asyncio.run(run())


def test_the_difference_names_the_files_that_changed(tmp_path):
    async def run():
        project = uuid4()
        workspace = workspace_with(tmp_path, project, {"backend/main.py": "app = 1\n",
                                                       "frontend/App.vue": "<template/>"})
        await history.record(tmp_path, project, workspace, "初回生成")
        workspace_with(tmp_path, project, {"backend/main.py": "app = 2\n"})
        (workspace / "frontend" / "App.vue").unlink()
        commit = await history.record(tmp_path, project, workspace, "整理した")
        diff = await history.difference(tmp_path, project, workspace, commit)
        assert {item["path"]: item["change"] for item in diff["files"]} == {
            "backend/main.py": "M", "frontend/App.vue": "D"}
        assert "app = 2" in diff["text"] and not diff["truncated"]
    asyncio.run(run())


def test_a_huge_difference_is_cut_and_says_so(tmp_path):
    """青天井に返すと、画面も通信も詰まる。"""
    async def run():
        project = uuid4()
        workspace = workspace_with(tmp_path, project, {"backend/main.py": "app = 1\n"})
        await history.record(tmp_path, project, workspace, "初回生成")
        workspace_with(tmp_path, project, {"backend/main.py": "x = 1\n" * 80_000})
        commit = await history.record(tmp_path, project, workspace, "大量に足した")
        diff = await history.difference(tmp_path, project, workspace, commit)
        assert diff["truncated"] and len(diff["text"].encode()) <= history.MAX_DIFF_BYTES
    asyncio.run(run())


def test_an_unknown_commit_does_not_reach_git(tmp_path):
    """外から来る値をそのまま git へ渡さない。"""
    async def run():
        project = uuid4()
        workspace = workspace_with(tmp_path, project, {"backend/main.py": "app = 1\n"})
        await history.record(tmp_path, project, workspace, "初回生成")
        for bad in ("../../etc", "HEAD --all", "", "a" * 64):
            assert (await history.difference(tmp_path, project, workspace, bad))["files"] == []
    asyncio.run(run())


def test_restoring_keeps_a_way_back(tmp_path):
    """戻す前を記録せずに戻すと、戻した操作そのものを取り消せなくなる。"""
    async def run():
        project = uuid4()
        workspace = workspace_with(tmp_path, project, {"backend/main.py": "app = 1\n"})
        good = await history.record(tmp_path, project, workspace, "動いていたころ")
        workspace_with(tmp_path, project, {"backend/main.py": "app = BROKEN\n",
                                           "backend/extra.py": "junk\n"})
        await history.record(tmp_path, project, workspace, "壊した")

        result = await history.restore(tmp_path, project, workspace, good)
        assert result["status"] == "restored"
        assert (workspace / "backend" / "main.py").read_text() == "app = 1\n"
        # 履歴は書き換えない。壊したときの記録も、戻した記録も残る。
        summaries = [row["summary"] for row in await history.entries(tmp_path, project, workspace)]
        assert "壊した" in summaries and any("戻した" in text for text in summaries)
    asyncio.run(run())


def test_an_unknown_point_cannot_be_restored(tmp_path):
    async def run():
        project = uuid4()
        workspace = workspace_with(tmp_path, project, {"backend/main.py": "app = 1\n"})
        await history.record(tmp_path, project, workspace, "初回生成")
        for bad in ("0" * 40, "../../etc", ""):
            assert (await history.restore(tmp_path, project, workspace, bad))["status"] == "unknown"
        assert (workspace / "backend" / "main.py").read_text() == "app = 1\n"
    asyncio.run(run())


def test_deleting_the_history_leaves_the_working_files(tmp_path):
    """履歴を消しても、いま動いているコードまで消してはいけない。"""
    async def run():
        project = uuid4()
        workspace = workspace_with(tmp_path, project, {"backend/main.py": "app = 1\n"})
        await history.record(tmp_path, project, workspace, "初回生成")
        assert await history.discard(tmp_path, project)
        assert not history.repository(tmp_path, project).exists()
        assert (workspace / "backend" / "main.py").read_text() == "app = 1\n"
        # 次の生成からまた記録しはじめる。
        assert await history.record(tmp_path, project, workspace, "作り直し")
    asyncio.run(run())


def test_history_for_an_app_that_never_generated_is_empty(tmp_path):
    project = uuid4()
    workspace = tmp_path / "projects" / str(project)
    assert asyncio.run(history.entries(tmp_path, project, workspace)) == []


def test_a_failure_to_record_is_reported_not_swallowed(tmp_path, caplog):
    """空文字だけ返して黙ると、置き場が消えていることに誰も気づけない。

    実際、履歴の置き場がPodの使い捨て領域に載っていて配備のたびに消えていたが、
    画面には「まだ記録がありません」としか出ず、初回との区別が付かなかった。
    """
    import logging

    async def run():
        project = uuid4()
        workspace = workspace_with(tmp_path, project, {"backend/main.py": "app = 1\n"})
        # 置き場を作れない状態にする（同じ名前のファイルが先にある）。
        repository = history.repository(tmp_path, project)
        repository.parent.mkdir(parents=True, exist_ok=True)
        repository.write_text("not a directory", encoding="utf-8")
        with caplog.at_level(logging.WARNING, logger="koyorina.history"):
            assert await history.record(tmp_path, project, workspace, "初回生成") == ""
        assert any("history_not_recorded" in record.message for record in caplog.records)
    asyncio.run(run())


def test_the_message_is_built_from_what_actually_changed(tmp_path):
    """件名は、記録する直前の変更を見てから決める。

    依頼文だけで決めると、追加なのか変更なのかが分からない。
    """
    async def run():
        project = uuid4()
        workspace = workspace_with(tmp_path, project, {"backend/main.py": "app = 1\n"})
        seen = []

        def compose(changes):
            seen.append(sorted(changes))
            return "feat: 初回生成\n"

        commit = await history.record(tmp_path, project, workspace, compose=compose)
        assert commit and seen == [[("A", "backend/main.py")]]
        rows = await history.entries(tmp_path, project, workspace)
        assert rows[0]["summary"] == "feat: 初回生成"

        # 2回目は変更として見える。
        workspace_with(tmp_path, project, {"backend/main.py": "app = 2\n"})
        seen.clear()
        await history.record(tmp_path, project, workspace, compose=compose)
        assert seen == [[("M", "backend/main.py")]]
    asyncio.run(run())


def test_the_agent_records_a_generation_with_the_model_subject(tmp_path):
    """生成の記録が、エージェントから実際に呼べること。

    メソッドがクラスから外れても、これまでのテストは全部通っていた。
    呼べるかどうかを誰も見ていなかったため。ここで通しておく。
    """
    from backend.domain.generation_progress import Progress
    from backend.domain.projects import ProjectInput
    from backend.worker.agent import Agent, AgentSettings, last_report

    async def run():
        project = uuid4()
        workspace = workspace_with(tmp_path, project, {"backend/main.py": "app = 1\n"})
        agent = Agent(AgentSettings(token="x" * 40, user_id=str(uuid4()), root=tmp_path))
        progress = Progress(tmp_path / "jobs" / "j", "j")
        progress.record("codex", "直しました。\n\nCOMMIT: fix: 一覧の日付が並ばないのを修正")
        payload = SimpleNamespace(
            project_id=project, job_id="66d03be2", instruction="日付が変なのを直して",
            requested_by="太郎", specification=ProjectInput(
                name="台帳", purpose="貸出を管理する", audience="team",
                fields=[{"name": "品名", "kind": "text", "required": True}]))

        assert last_report(progress).startswith("直しました")
        await agent.record_history(payload, workspace, progress)

        rows = await history.entries(tmp_path, project, workspace)
        assert rows and rows[0]["summary"] == "fix: 一覧の日付が並ばないのを修正"
        assert rows[0]["author"] == "太郎"
        # 本文には変わったものと出どころが残る。
        diff = await history.difference(tmp_path, project, workspace, rows[0]["commit"])
        assert [item["path"] for item in diff["files"]] == ["backend/main.py"]
        # 完了の知らせも出す。黙って終えると、記録できたのか分からない。
        assert any("履歴へ記録しました" in event["message"]
                   for event in progress.data["events"])
    asyncio.run(run())
