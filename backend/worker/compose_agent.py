"""Docker Compose版のエージェント起動役。k8s版の「設定が変わったらPodを作り直す」に当たる。

controller が画面の生成AI設定を <settings_dir>/agent-env.json に書く。ここではそれを
環境変数へ重ねてエージェント（uvicorn）を起動し、ファイルが変わったら、生成・対話が
進行中でないことを確かめてから起動し直す。進行中に止めると作業が失われるため待つ。

compose.yaml の環境変数・secrets が既定値で、ファイルの値がそれを上書きする。
身元（AGENT_USER_ID 等）は controller 側でファイルへ書かないので、ここでも変わらない。
"""
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import urllib.request

COMMAND = ["uvicorn", "backend.worker.agent:create_agent", "--factory", "--host", "0.0.0.0",
           "--port", "8080", "--no-access-log", "--no-proxy-headers"]
SETTINGS = Path(os.environ.get("COMPOSE_AGENT_SETTINGS", "/settings/agent-env.json"))
POLL_SECONDS = 3


def fingerprint() -> str:
    return hashlib.sha256(SETTINGS.read_bytes()).hexdigest() if SETTINGS.is_file() else ""


def environment() -> dict[str, str]:
    overlay = json.loads(SETTINGS.read_text()) if SETTINGS.is_file() else {}
    return {**os.environ, **{str(k): str(v) for k, v in overlay.items()}}


def agent_token() -> str:
    secrets_dir = os.environ.get("KOYORINA_SECRETS_DIR")
    if secrets_dir and (Path(secrets_dir) / "agent_token").is_file():
        return (Path(secrets_dir) / "agent_token").read_text().strip()
    return os.environ.get("AGENT_TOKEN", "")


def busy() -> bool:
    """生成・対話が進行中か。答えが得られないときは「進行中」とみなし、止めない。"""
    request = urllib.request.Request("http://127.0.0.1:8080/runtime",
                                     headers={"Authorization": "Bearer " + agent_token()})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return bool(json.load(response).get("busy"))
    except (OSError, ValueError):
        return True


def stop(child: subprocess.Popen):
    child.terminate()
    try:
        child.wait(timeout=20)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait()


def main():
    current = fingerprint()
    child = subprocess.Popen(COMMAND, env=environment())

    def forward(signum, _frame):
        stop(child)
        sys.exit(0)
    signal.signal(signal.SIGTERM, forward)
    signal.signal(signal.SIGINT, forward)

    while True:
        time.sleep(POLL_SECONDS)
        if child.poll() is not None:
            sys.exit(child.returncode)  # エージェント自体が落ちた。composeの再起動に任せる。
        latest = fingerprint()
        if latest == current or busy():
            continue
        print("compose_agent: 生成AIの設定が変わったため、エージェントを起動し直します。", flush=True)
        stop(child)
        current = latest
        child = subprocess.Popen(COMMAND, env=environment())


if __name__ == "__main__":
    main()
