"""ローカル検証専用のプレビュー実行基盤。固定の引数だけでDockerを操作する。

Dockerデーモンへの接続はホストのroot相当の権限にあたる。APP_ENV=local かつ
PREVIEW_ENABLED=true のときだけ有効にし、共有環境では使用しない。
生成コードはこのコンテナの中だけで動かし、KoyorinaのDB・Codex認証情報は渡さない。
"""
import asyncio
import os
from uuid import UUID
import httpx
from fastapi import HTTPException

UNAVAILABLE = "プレビュー実行環境へ接続できません。Dockerの起動状態を管理者に確認してください。"


def container_name(project_id) -> str:
    return "koyorina-preview-" + UUID(str(project_id)).hex


async def docker(*args, environment=None, timeout=60):
    try:
        process = await asyncio.create_subprocess_exec(
            "docker", *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env={**os.environ, **(environment or {})})
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout)
    except (OSError, asyncio.TimeoutError):
        raise HTTPException(503, UNAVAILABLE) from None
    # Dockerの標準エラーはホストのパスや設定を含む。利用者へ返さない。
    return process.returncode, stdout.decode("utf-8", "replace"), stderr.decode("utf-8", "replace")


async def available() -> bool:
    code, _, _ = await docker("version", "--format", "{{.Server.Version}}", timeout=15)
    return code == 0


async def container_state(project_id) -> str:
    """none / starting / running / exited を返す。存在しないコンテナはエラーにしない。

    created と restarting は「これから動く」状態。exited と同じ扱いにすると、
    起動した直後のほんの一瞬を異常終了として画面に出すことになる。
    """
    code, stdout, _ = await docker("inspect", "--format", "{{.State.Status}}",
                                   container_name(project_id), timeout=20)
    if code != 0:
        return "none"
    status = stdout.strip()
    if status == "running":
        return "running"
    return "starting" if status in {"created", "restarting"} else "exited"


async def responding(port: int, host: str = "127.0.0.1") -> bool:
    try:
        async with httpx.AsyncClient(timeout=3, trust_env=False) as client:
            response = await client.get(f"http://{host}:{port}/__preview/health", follow_redirects=False)
    except httpx.HTTPError:
        return False
    return response.status_code < 500


async def remove(project_id):
    await docker("rm", "--force", container_name(project_id), timeout=30)


async def run(project_id, paths, port: int, image: str, environment: dict, network: str = ""):
    """既存コンテナを破棄して起動し直す。値はargvへ出さず親プロセスの環境から渡す。

    network を渡すと（Docker Compose版）、ホストへポートを出さずにその網へ繋ぐ。
    管理アプリ自身がコンテナの中にいて、ホストの127.0.0.1へは届かないため。
    """
    await remove(project_id)
    arguments = [
        "run", "--detach", "--name", container_name(project_id),
        "--label", "koyorina-preview=1",
        *(["--network", network] if network else ["--publish", f"127.0.0.1:{int(port)}:8080"]),
        "--volume", f"{paths.workspace}:/workspace",
        "--volume", f"{paths.var}:/var/preview",
        "--workdir", "/workspace",
        "--user", f"{os.getuid()}:{os.getgid()}",
        "--memory", "1g", "--cpus", "2", "--pids-limit", "512",
        "--security-opt", "no-new-privileges", "--cap-drop", "ALL",
        "--restart", "no",
    ]
    for key in sorted(environment):
        arguments += ["--env", key]
    arguments.append(image)
    code, _, _ = await docker(*arguments, environment=environment, timeout=120)
    if code != 0:
        raise HTTPException(503, "プレビューを起動できませんでした。ランタイムイメージの作成状況を確認してください。")


async def logs(project_id, lines: int = 200) -> str:
    code, stdout, stderr = await docker("logs", "--tail", str(int(lines)), container_name(project_id), timeout=30)
    if code != 0:
        return ""
    # 生成アプリの出力は信用しない文字列として扱う。画面ではテキストのまま表示する。
    return (stdout + stderr)[-20000:]


async def execute(project_id, command: str) -> dict:
    """ローカル検証用。共有環境の経路は preview_controller が持つ。

    打ち切りは docker 側に渡さず、こちらの待ち時間で止める。docker exec は
    待つのをやめてもコンテナの中では動き続けるので、結果を捨てるだけになる。
    """
    code, stdout, stderr = await docker("exec", container_name(project_id), "/bin/sh", "-c",
                                        command, timeout=120)
    return {"command": command, "stdout": stdout[-40000:], "stderr": stderr[-40000:],
            "exit_code": code}
