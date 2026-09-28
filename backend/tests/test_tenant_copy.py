"""テナント移行のコピー。移行用Podで動くスクリプトを、手元の一時フォルダで実際に動かす。"""
import os
import shutil
import subprocess
import sys

import pytest

from backend.domain.tenant_copy import copy_script

PROJECT = "85689fb4-db61-4aae-8fe1-48a8c77745a7"
JOB = "72509cdd-2e24-4002-88a4-221507bacd38"
ITEMS = [(f"projects/{PROJECT}", True), (f"history/{PROJECT}.git", False), (f"jobs/{JOB}", False)]


def tree(root):
    project = root / "projects" / PROJECT
    (project / "frontend" / "src").mkdir(parents=True)
    (project / "frontend" / "src" / "App.vue").write_text("<template>app</template>")
    (project / "README.md").write_text("readme")
    (project / "frontend" / "node_modules" / "vue").mkdir(parents=True)
    (project / "frontend" / "node_modules" / "vue" / "index.js").write_text("derived")
    history = root / "history" / f"{PROJECT}.git"
    (history / "refs" / "tags").mkdir(parents=True)  # 空のフォルダ（gitが要る）
    (history / "refs" / "heads").mkdir()
    (history / "refs" / "heads" / "main").write_text("0" * 40)
    (history / "HEAD").write_text("ref: refs/heads/main\n")
    (root / "jobs" / JOB).mkdir(parents=True)
    (root / "jobs" / JOB / "status.json").write_text('{"status": "generated"}')
    return project


def run(source, target):
    return subprocess.run([sys.executable, "-c", copy_script(ITEMS, str(source), str(target))],
                          capture_output=True, text=True)


def test_code_history_and_jobs_are_copied_and_verified(tmp_path):
    source, target = tmp_path / "source", tmp_path / "target"
    tree(source)
    target.mkdir()
    result = run(source, target)
    assert result.returncode == 0, result.stderr
    moved = target / "projects" / PROJECT
    assert (moved / "frontend" / "src" / "App.vue").read_text() == "<template>app</template>"
    assert not (moved / "frontend" / "node_modules").exists()  # 依存の生成物は移さない
    assert (target / "history" / f"{PROJECT}.git" / "refs" / "tags").is_dir()
    assert (target / "jobs" / JOB / "status.json").is_file()
    # もう一度流しても同じ結果になる（途中で失敗した移行のやり直し）。
    assert run(source, target).returncode == 0


def test_links_inside_the_app_move_as_links(tmp_path):
    """以前はフォルダへのリンクが1つあるだけで「copy verification failed」になっていた。

    生成アプリではよくある形（backend/static -> ../frontend/dist）。リンクのまま移す。
    """
    source, target = tmp_path / "source", tmp_path / "target"
    project = tree(source)
    (project / "frontend" / "dist").mkdir()
    (project / "frontend" / "dist" / "index.html").write_text("<html></html>")
    (project / "backend").mkdir()
    os.symlink("../frontend/dist", project / "backend" / "static")
    os.symlink("README.md", project / "LATEST.md")
    target.mkdir()
    result = run(source, target)
    assert result.returncode == 0, result.stderr
    moved = target / "projects" / PROJECT
    assert os.readlink(moved / "backend" / "static") == "../frontend/dist"
    assert (moved / "backend" / "static" / "index.html").read_text() == "<html></html>"
    assert os.readlink(moved / "LATEST.md") == "README.md"


@pytest.mark.parametrize("pointing", ["absolute", "outside"])
def test_links_leaving_the_app_are_refused_by_name(tmp_path, pointing):
    """外を指すリンクは別テナントの領域を指せる。移さず、どれかを示して止める。"""
    source, target = tmp_path / "source", tmp_path / "target"
    project = tree(source)
    (tmp_path / "elsewhere").mkdir()
    link = (tmp_path / "elsewhere") if pointing == "absolute" else "../../../elsewhere"
    os.symlink(link, project / "frontend" / "linked")
    target.mkdir()
    result = run(source, target)
    assert result.returncode != 0
    assert "symlink is not migratable: frontend/linked" in result.stderr
    assert "copy verification failed" not in result.stderr
    assert not (target / "projects" / PROJECT / "frontend" / "linked").exists()


def test_links_inside_derived_folders_are_ignored(tmp_path):
    """.venv や node_modules にはリンクが普通にある。そこは移さないので止めない。"""
    source, target = tmp_path / "source", tmp_path / "target"
    project = tree(source)
    os.symlink("index.js", project / "frontend" / "node_modules" / "vue" / "alias.js")
    target.mkdir()
    assert run(source, target).returncode == 0


def test_a_mismatch_names_the_paths_but_not_the_contents(tmp_path, monkeypatch, capsys):
    """移行中に書き込まれた等で食い違ったら、どのパスかをログに出す。中身は出さない。"""
    source, target = tmp_path / "source", tmp_path / "target"
    tree(source)
    target.mkdir()
    real = shutil.copy2

    def copy_then_change(src, dst, **kwargs):
        real(src, dst, **kwargs)
        if str(src).endswith("status.json"):
            with open(src, "w") as stream:  # 移行元がコピーの後に書き換わった
                stream.write('{"status": "failed", "note": "private text"}')
    monkeypatch.setattr(shutil, "copy2", copy_then_change)
    with pytest.raises(RuntimeError, match="copy verification failed"):
        exec(compile(copy_script(ITEMS, str(source), str(target)), "<copy>", "exec"), {})
    error = capsys.readouterr().err
    assert "changed: status.json" in error and "private text" not in error


def test_preview_rebuilds_its_environment_instead_of_moving_it(tmp_path):
    """プレビューの仮想環境はイメージのPythonを絶対パスで指す。以前はここで
    「symlink is not migratable: var/venv/bin/python」になっていた。

    作り直せる物は移さない。導入済みの印（*.sha）も移さず、移行先で入れ直させる。
    """
    from backend.worker.preview_controller import PREVIEW_REBUILT
    source, target = tmp_path / "source", tmp_path / "target"
    preview = source / PROJECT
    (preview / "workspace").mkdir(parents=True)
    (preview / "workspace" / "main.py").write_text("app")
    (preview / "var" / "venv" / "bin").mkdir(parents=True)
    os.symlink("/usr/local/bin/python3", preview / "var" / "venv" / "bin" / "python")
    (preview / "var" / "cache" / "uv").mkdir(parents=True)
    (preview / "var" / "python.sha").write_text("digest")
    (preview / "var" / "node.sha").write_text("digest")
    (preview / "var" / "home").mkdir()
    (preview / "state.json").write_text("{}")
    target.mkdir()
    result = subprocess.run([sys.executable, "-c", copy_script(
        [(PROJECT, True, PREVIEW_REBUILT)], str(source), str(target))], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    moved = target / PROJECT
    assert (moved / "workspace" / "main.py").read_text() == "app"
    assert (moved / "state.json").is_file() and (moved / "var" / "home").is_dir()
    for name in PREVIEW_REBUILT:
        assert not (moved / name).exists() and not (moved / name).is_symlink(), name
