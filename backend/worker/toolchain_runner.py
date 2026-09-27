"""依存の導入と検査を実行する。組み立ては backend/domain/toolchain.py。

ここが持つのは「外のプロセスを起こして、待って、出力を受け取る」部分だけ。
何を走らせるかの判断はドメイン側にあるので、判断の試験に実行環境は要らない。
"""
import asyncio
import logging
import os
import signal
import time
from pathlib import Path

from backend.domain.toolchain import (INSTALL_TIMEOUT, VERIFY_TIMEOUT, Step, argv_for,
                                      failure_detail, failure_summary, install_steps,
                                      SANDBOX_PROBE, SANDBOX_PROBE_TIMEOUT, inventory_steps,
                                      missing_tooling, sandbox_unavailable, verification_steps)

logger = logging.getLogger("uvicorn.error")
# 出力は受け取るが、際限なく溜めない。失敗の理由は末尾に出る。
MAX_CAPTURE = 200_000
TRUSTED_CA_ENVIRONMENT = ("SSL_CERT_FILE", "NODE_EXTRA_CA_CERTS", "REQUESTS_CA_BUNDLE")


def _step_report(step: Step) -> str:
    """検査の種類だけを出す。生成物由来の引数や出力は表示しない。"""
    command = step.argv[0].rsplit("/", 1)[-1]
    if command == "vite":
        command = "vite build"
    elif command == "vue-tsc":
        command = "vue-tsc --noEmit"
    elif command == "pytest":
        command = "pytest"
    return f"{step.label}（{command}、最大{step.timeout}秒）"


async def _run(workspace: Path, step: Step, environment: dict, timeout: int, writable=()):
    # 基盤が設定したCAファイルは読取専用rootから参照する。明示しておくことで、
    # 将来rootの見せ方を狭めても導入用CAを落とさない。
    ca_paths = tuple(dict.fromkeys(
        environment[key] for key in TRUSTED_CA_ENVIRONMENT if environment.get(key)))
    argv = argv_for(workspace, step, writable, readable=ca_paths)
    directory = workspace / step.cwd if step.cwd else workspace
    try:
        process = await asyncio.create_subprocess_exec(
            *argv, cwd=str(directory), env=environment,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            start_new_session=True)
    except OSError as exc:
        logger.warning("toolchain_spawn_failed %s", exc.__class__.__name__)
        return 127, f"{argv[0]} を起動できませんでした。"
    try:
        # 出力読み取りと終了待ちに別々timeoutを与えると、
        # 最悪2倍待つ。1つの期限で両方が終わることを要求する。
        async with asyncio.timeout(timeout):
            output, _ = await asyncio.gather(_drain(process.stdout), process.wait())
    except asyncio.TimeoutError:
        # bwrap -> pytest と子プロセスが連なる。親だけを
        # killすると子がpipeを握ったまま残るため、グループ全体を止める。
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await process.wait()
        return 124, f"{timeout}秒を超えたため打ち切りました。"
    return process.returncode, output.decode("utf-8", "replace")


async def _drain(stream) -> bytes:
    """EOFまで読む。上限を超えたぶんは捨てるが、読むのはやめない。

    StreamReader.read(n) は「nバイトまで」であって「nバイト読む」ではない。
    1バイトでも来た時点で返るので、これで済ませると最初の1回ぶんしか取れない
    （サンドボックスが先に警告を1行出すため、それだけを拾って残りを捨てていた）。
    途中で読むのをやめるとパイプが詰まって子プロセスが終われなくなるので、
    上限に達しても最後まで読み切る。
    """
    chunks, total = [], 0
    while True:
        chunk = await stream.read(65536)
        if not chunk:
            break
        total += len(chunk)
        if total <= MAX_CAPTURE:
            chunks.append(chunk)
    return b"".join(chunks)


def install_environment(base: dict, packages: dict, cache: Path | None = None) -> dict:
    """取得元は基盤が決める。生成側の宣言では差し替えられないようにする。"""
    # Ciliumの監査CAをNode・Python・uvへ渡す。これを落とすとPod本体の
    # HTTPS疎通は成功しても、絞り込んだ導入用環境のnpm/uvだけが証明書を
    # 検証できず UNABLE_TO_VERIFY_LEAF_SIGNATURE になる。
    environment = {key: value for key, value in base.items()
                   if key in ("PATH", "HOME", "LANG", "TMPDIR", "CODEX_HOME",
                              *TRUSTED_CA_ENVIRONMENT)}
    environment.update(packages)
    # 取得だけをさせる。npm の対話とテレメトリは切る。
    environment.update({"npm_config_yes": "true", "NPM_CONFIG_FUND": "false",
                        "NPM_CONFIG_AUDIT": "false", "CI": "true"})
    if cache is not None:
        # 既定では HOME の下に溜まる。生成PodのHOMEはコンテナのディスクで、
        # ephemeral-storage の上限（数百MB）を依存の取得だけで超える。超えると
        # Podがノードから追い出され、生成が理由の分からない中断として出る。
        # 共有PVC側の、作業場所の外に置く（成果物にも履歴にも入らない場所）。
        cache.mkdir(mode=0o700, parents=True, exist_ok=True)
        home = cache / "home"
        home.mkdir(mode=0o700, exist_ok=True)
        environment.update({"UV_CACHE_DIR": str(cache / "uv"),
                            "NPM_CONFIG_CACHE": str(cache / "npm"),
                            # Podは readOnlyRootFilesystem。既定のHOMEへ書けず、
                            # uv/npmなどが既定HOMEへキャッシュを書こうとすると、
                            # 読取専用rootで失敗する。作業用HOMEを明示しておく。
                            "HOME": str(home), "CODEX_HOME": str(home / "codex")})
        (home / "codex").mkdir(mode=0o700, exist_ok=True)
    return environment


async def install(workspace: Path, packages: dict, report=None, cache: Path | None = None) -> list[str]:
    """宣言どおりに依存を入れる。戻り値は失敗の詳細（空なら成功）。"""
    environment = install_environment(dict(os.environ), packages, cache)
    problems = []
    for step in install_steps(workspace):
        if report:
            report(step.label)
        code, output = await _run(workspace, step, environment, INSTALL_TIMEOUT,
                                  writable=(cache,) if cache else ())
        if code:
            problems.append(failure_detail(step, code, output))
            if report:
                report(failure_summary(step), failed=True)
            # 片方が入らなくても、もう片方は入れておく。直す材料を一度に集める。
    return problems


async def verify(workspace: Path, report=None, cache: Path | None = None) -> list[str]:
    """入ったもので実際に動かす。サンドボックスの中なので通信はできない。"""
    environment = install_environment(dict(os.environ), {}, cache)
    # 走らせられなかった検査を、通ったことにしない。
    problems = missing_tooling(workspace)
    for step in verification_steps(workspace):
        if report:
            report(_step_report(step))
        started = time.monotonic()
        code, output = await _run(workspace, step, environment, step.timeout)
        elapsed = time.monotonic() - started
        if code:
            problems.append(failure_detail(step, code, output))
            if report:
                report(f"{failure_summary(step)}（経過 {elapsed:.1f}秒）", failed=True)
        elif report:
            report(f"{step.label}が完了しました（経過 {elapsed:.1f}秒）。")
    return problems


async def inventory(workspace: Path, cache: Path | None = None) -> str:
    """入った部品とその版。ジョブの記録として残す。"""
    environment = install_environment(dict(os.environ), {}, cache)
    parts = []
    for step in inventory_steps(workspace):
        code, output = await _run(workspace, step, environment, VERIFY_TIMEOUT)
        if not code:
            parts.append(f"# {' '.join(step.argv)}\n{output.strip()}")
    return "\n\n".join(parts)


async def sandbox_problem(workspace: Path, cache: Path | None = None) -> str:
    """検査の前に、閉じ込めが使えるかだけ確かめる。使えないなら理由を返す。

    使えないまま検査すると、何を走らせても落ちる。その出力をモデルへ戻すと、
    アプリの不具合だと思って直そうとし、直らないまま回数を使い切る。
    """
    environment = install_environment(dict(os.environ), {}, cache)
    code, output = await _run(workspace, SANDBOX_PROBE, environment, SANDBOX_PROBE_TIMEOUT)
    return sandbox_unavailable(code, output)
