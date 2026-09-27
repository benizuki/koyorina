"""生成コードの履歴。アプリごとに1つのGitリポジトリへ、生成のたびに記録する。

リポジトリの実体は**作業場所の外**に置き、作業場所には指し示すだけのファイルを置く。

    /data/projects/<アプリID>/.git   「gitdir: ...」と書いてあるだけのファイル
    /data/history/<アプリID>.git     履歴の実体

こうすると、生成モデルは作業場所で普通に `git log` や `git diff` を使える（不具合を
追うときに効く）。一方で実体は書き込み権限の外にあるので、`git reset --hard` や
`.git` ごと削除されても**履歴は失われない**。指し示すファイルが消えても、次の記録で
作り直す。

読めるようにするには、実体の場所を生成サンドボックスの「読み取り可」へ加える必要が
ある（backend/domain/generation.py の generation_permission_args）。

記録に失敗しても生成は止めない。履歴のために生成を落とすのは本末転倒なので、
呼び出し側は結果を見て記録の成否だけを扱う。
"""
import asyncio
import logging
from pathlib import Path
from uuid import UUID

logger = logging.getLogger("koyorina.history")

# 作業場所に混ざる派生物は記録しない。生成物の判定と同じ並びにしておく。
EXCLUDED = ("__pycache__/", ".pytest_cache/", ".mypy_cache/", ".ruff_cache/",
            "node_modules/", "dist/", ".venv/", "attachments/", ".git", ".agents/",
            ".koyorina-tmp/",
            # pytest の一時ファイル。サンドボックスは /tmp に書けないので、
            # Python が作業場所へ退避する。毎回名前が変わり、差分が読めなくなる。
            "pytest-of-*/")
# 差分は画面へ返す。青天井に返すと、画面も通信も詰まる。
MAX_DIFF_BYTES = 200_000
MAX_ENTRIES = 200
IDENTITY = ("-c", "user.name=Koyorina", "-c", "user.email=koyorina@localhost",
            "-c", "commit.gpgsign=false", "-c", "core.quotePath=false")


def repository(root: Path, project_id) -> Path:
    return root / "history" / (str(UUID(str(project_id))) + ".git")


def environment(repo: Path) -> dict:
    # 生成コードの中の設定を拾わない。履歴の付け方を成果物に左右させない。
    return {"HOME": str(repo.parent), "PATH": "/usr/bin:/bin:/usr/local/bin",
            "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
            "GIT_TERMINAL_PROMPT": "0"}


async def call(repo: Path, *arguments, timeout=60):
    """作業ツリーを指さない操作（init など）。--work-tree と併用できない。"""
    try:
        process = await asyncio.create_subprocess_exec(
            "git", *IDENTITY, *arguments, env=environment(repo),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await asyncio.wait_for(process.communicate(), timeout)
    except (OSError, asyncio.TimeoutError):
        return 1, "", "git unavailable"
    return process.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


async def run(repo: Path, workspace: Path, *arguments, timeout=60):
    """gitを作業場所の外のリポジトリで動かす。作業ツリーだけを指し示す。"""
    try:
        process = await asyncio.create_subprocess_exec(
            "git", f"--git-dir={repo}", f"--work-tree={workspace}", *IDENTITY, *arguments,
            env=environment(repo),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await asyncio.wait_for(process.communicate(), timeout)
    except (OSError, asyncio.TimeoutError):
        return 1, "", "git unavailable"
    return process.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


def failed(step: str, repo: Path, error: str) -> str:
    """記録できなかった理由を運用のログへ残す。生成は止めない。

    黙って空文字を返していたため、置き場が消えていること自体に誰も気づけなかった。
    画面には「まだ記録がありません」としか出ず、初回なのか壊れているのか分からない。
    """
    logger.warning("history_not_recorded %s", {"step": step, "repo": str(repo),
                                               "error": error.strip()[:300]})
    return ""


def link(repo: Path, workspace: Path) -> None:
    """作業場所から実体を指す。モデルが消しても、ここで作り直す。"""
    pointer = workspace / ".git"
    text = f"gitdir: {repo}\n"
    try:
        if pointer.is_dir():
            return  # 自分で作った本物のリポジトリがある。壊さない。
        if not pointer.is_file() or pointer.read_text(encoding="utf-8", errors="replace") != text:
            pointer.write_text(text, encoding="utf-8")
    except OSError as error:
        failed("link", repo, str(error))


async def prepare(repo: Path, workspace: Path) -> bool:
    """置き場を用意する。ここで例外を外へ出さない。

    履歴のために生成を落とすのは本末転倒。置けなかったという事実だけを返す。
    """
    try:
        if (repo / "HEAD").is_file():
            link(repo, workspace)
            return True
        repo.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError as error:
        return bool(failed("mkdir", repo, str(error)))
    if (await call(repo, "init", "--bare", "--initial-branch=main", str(repo)))[0]:
        return False
    # 作業場所を作業ツリーとして結びつける。これで作業場所から素のgitが使える。
    await call(repo, f"--git-dir={repo}", "config", "core.bare", "false")
    await call(repo, f"--git-dir={repo}", "config", "core.worktree", str(workspace))
    # 派生物は記録しない。.gitignore を作業場所へ置くと、それ自体が成果物に混ざる。
    (repo / "info").mkdir(exist_ok=True)
    (repo / "info" / "exclude").write_text("\n".join(EXCLUDED) + "\n", encoding="utf-8")
    link(repo, workspace)
    return True


async def staged(repo: Path, workspace: Path):
    """これから記録する変更の一覧。件名を組み立てるために先に読む。"""
    code, out, _ = await run(repo, workspace, "diff", "--cached", "--name-status")
    changes = []
    if code:
        return changes
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            changes.append((parts[0][:1], parts[-1]))
    return changes


async def record(root: Path, project_id, workspace: Path, message: str = "", author: str = "",
                 compose=None) -> str:
    """いまの作業場所を1件として記録する。変化が無ければ何も残さない。

    compose を渡すと、実際に変わったファイルを見てから件名を組み立てられる。
    渡さなければ message をそのまま使う（戻す操作など、内容が先に決まる記録）。

    戻り値はコミットID。記録できなかったときは空文字。呼び出し側は止まらない。
    """
    repo = repository(root, project_id)
    if not workspace.is_dir():
        return failed("workspace", repo, "作業場所がありません")
    if not await prepare(repo, workspace):
        return failed("prepare", repo, "履歴の置き場を用意できませんでした")
    code, _, error = await run(repo, workspace, "add", "--all")
    if code:
        return failed("add", repo, error)
    # 変化が無ければコミットしない。同じ内容の記録が並ぶと、履歴が読めなくなる。
    if (await run(repo, workspace, "diff", "--cached", "--quiet"))[0] == 0:
        return ""
    text = compose(await staged(repo, workspace)) if compose else message
    arguments = ["commit", "--no-verify", "-m", text[:2000]]
    if author:
        arguments += ["--author", author[:200]]
    code, _, error = await run(repo, workspace, *arguments, timeout=120)
    if code:
        return failed("commit", repo, error)
    code, out, error = await run(repo, workspace, "rev-parse", "HEAD")
    return out.strip() if code == 0 else failed("rev-parse", repo, error)


def known(commit: str) -> bool:
    """外から来た指定をそのまま git へ渡さない。"""
    return commit.isalnum() and 7 <= len(commit) <= 40


async def restore(root: Path, project_id, workspace: Path, commit: str) -> dict:
    """その時点の中身へ作業場所を戻す。戻す前の状態も記録してから行う。

    記録せずに戻すと、戻した操作そのものを取り消せない。壊す方向の操作なので、
    必ず退路を残してから動かす。
    """
    repo = repository(root, project_id)
    if not (repo / "HEAD").is_file() or not known(commit):
        return {"status": "unknown", "commit": commit, "saved": "", "restored": ""}
    if not await prepare(repo, workspace):
        return {"status": "failed", "commit": commit, "saved": "", "restored": ""}
    if (await run(repo, workspace, "cat-file", "-e", commit + "^{commit}"))[0]:
        return {"status": "unknown", "commit": commit, "saved": "", "restored": ""}
    saved = await record(root, project_id, workspace, "chg: 戻す前の状態を記録")
    # 作業ツリーだけを入れ替える。履歴は進める方向にしか動かさない。
    if (await run(repo, workspace, "checkout", "--force", commit, "--", "."))[0]:
        return {"status": "failed", "commit": commit, "saved": saved, "restored": ""}
    # 戻した結果を新しい1件として残す。履歴を書き換えないので、さらに戻せる。
    restored = await record(root, project_id, workspace,
                        f"chg: {commit[:7]} の内容へ戻した")
    return {"status": "restored", "commit": commit, "saved": saved, "restored": restored}


async def discard(root: Path, project_id) -> bool:
    """履歴そのものを消す。取り消せないので、呼ぶ側で必ず承認を取ること。"""
    import shutil
    repo = repository(root, project_id)
    if not repo.exists():
        return True
    shutil.rmtree(repo, ignore_errors=True)
    return not repo.exists()


async def entries(root: Path, project_id, workspace: Path, limit: int = 50) -> list[dict]:
    repo = repository(root, project_id)
    if not (repo / "HEAD").is_file():
        return []
    separator = "\x1f"
    code, out, error = await run(repo, workspace, "log", f"--max-count={min(limit, MAX_ENTRIES)}",
                                 f"--pretty=format:%H{separator}%aI{separator}%an{separator}%s")
    if code:
        failed("log", repo, error)
        return []
    result = []
    for line in out.splitlines():
        parts = line.split(separator)
        if len(parts) == 4:
            result.append({"commit": parts[0], "at": parts[1], "author": parts[2],
                           "summary": parts[3]})
    return result


async def difference(root: Path, project_id, workspace: Path, commit: str) -> dict:
    """1件ぶんの差分。ひとつ前との比較で、最初の記録は全体を出す。"""
    repo = repository(root, project_id)
    if not (repo / "HEAD").is_file() or not known(commit):
        return {"commit": commit, "text": "", "truncated": False, "files": []}
    code, names, _ = await run(repo, workspace, "show", "--name-status", "--pretty=format:", commit)
    code2, text, _ = await run(repo, workspace, "show", "--pretty=format:", "--no-color", commit)
    if code or code2:
        return {"commit": commit, "text": "", "truncated": False, "files": []}
    body = text.encode("utf-8")[:MAX_DIFF_BYTES].decode("utf-8", "ignore")
    files = []
    for line in names.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            files.append({"change": parts[0][:1], "path": parts[-1][:200]})
    return {"commit": commit, "text": body, "truncated": len(text.encode("utf-8")) > MAX_DIFF_BYTES,
            "files": files[:200]}
