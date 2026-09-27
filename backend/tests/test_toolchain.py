"""依存の導入と検査。生成中に「本当に動くか」を確かめるための仕組み。"""
import asyncio
import json
import os
from pathlib import Path

import pytest

from backend.domain.toolchain import (PYTEST_TIMEOUT, SENSITIVE_ROOTS, VERIFY_TIMEOUT, Step, argv_for,
                                      failure_detail, install_steps, verification_steps)
from backend.worker import toolchain_runner


def app(tmp_path, *, python=True, node=True, tests=False, name="workspace") -> Path:
    workspace = tmp_path / name
    (workspace / "frontend").mkdir(parents=True, exist_ok=True)
    if python:
        (workspace / "pyproject.toml").write_text(
            '[project]\nname = "x"\ndependencies = ["fastapi", "uvicorn"]\n', encoding="utf-8")
    if node:
        (workspace / "frontend" / "package.json").write_text(
            json.dumps({"scripts": {"build": "vite build"}}), encoding="utf-8")
    if tests:
        (workspace / "test_app.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    return workspace


def test_installation_cannot_reach_other_applications(tmp_path):
    """生成Podは /data/projects を丸ごとマウントしている。

    Podは利用者ごとで、その人の全アプリの依頼を受け持つので、マウントを1アプリに
    絞ることはできない。素のプロセスとして導入すると、他のアプリの作業場所にも
    手が届く位置で動く。だから導入もサンドボックスの中で走らせる。
    """
    workspace = app(tmp_path)
    cache = tmp_path / "cache"
    cache.mkdir()
    for step in install_steps(workspace):
        assert step.confined, f"{step.argv[0]} がサンドボックスの外で動く"
        argv = argv_for(workspace, step, writable=(cache,))
        assert argv[0] == "bwrap"
        # 触れるのはこのアプリの作業場所と、取得キャッシュだけ。
        assert ["--ro-bind", "/", "/"] == argv[argv.index("--ro-bind"):argv.index("--ro-bind") + 3]
        assert ["--bind", str(workspace.resolve()), str(workspace.resolve())] in [
            argv[index:index + 3] for index in range(len(argv) - 2)]
        assert ["--bind", str(cache.resolve()), str(cache.resolve())] in [
            argv[index:index + 3] for index in range(len(argv) - 2)]
        assert {"/proc", "/app", "/run/vertex", "/data"} <= set(SENSITIVE_ROOTS)


def test_only_the_install_can_see_the_pod_network(tmp_path):
    """検査はsocketを使えるが、別netnsに入れて外部回線は見せない。"""
    workspace = app(tmp_path, tests=True)
    binaries = workspace / "frontend" / "node_modules" / ".bin"
    binaries.mkdir(parents=True)
    (binaries / "vite").touch()
    (workspace / ".venv" / "bin").mkdir(parents=True)
    (workspace / ".venv" / "bin" / "pytest").touch()

    fetching = [s for s in install_steps(workspace) if s.argv[0] != "uv" or "install" in s.argv]
    assert all("--unshare-net" not in argv_for(workspace, step) for step in fetching)
    # 検査は全て別network namespaceへ入れる。Unix socketは使えるため、
    # FastAPI TestClient/asyncioを止めずに外部通信だけ遮断できる。
    pytest_step = next(step for step in verification_steps(workspace)
                       if step.argv[0].endswith("pytest"))
    assert argv_for(workspace, pytest_step)[0] == "bwrap"
    assert "--unshare-net" in argv_for(workspace, pytest_step)


def test_installation_never_executes_third_party_code(tmp_path):
    """導入の時点で、依頼したアプリのライブラリのコードを走らせない。

    ローカルPCと違い、生成Podは全員で共有するノードにあり、Codexの資格情報も
    同じPodにある。postinstall や setup.py がそこで動く形にはしない。
    """
    steps = install_steps(app(tmp_path))
    npm = next(s for s in steps if s.argv[0] == "npm")
    assert "--ignore-scripts" in npm.argv
    # package-lock.json は成果物として受け取れない名前。作らせない。
    assert "--no-package-lock" in npm.argv
    install = next(s for s in steps if s.argv[:3] == ["uv", "pip", "install"])
    assert install.argv[install.argv.index("--only-binary") + 1] == ":all:"


def test_only_the_declared_side_is_installed(tmp_path):
    assert [s.argv[0] for s in install_steps(app(tmp_path, node=False, name="a"))] == ["uv", "uv"]
    assert [s.argv[0] for s in install_steps(app(tmp_path, python=False, name="b"))] == ["npm"]
    assert install_steps(app(tmp_path, python=False, node=False, name="c")) == []


def test_verification_runs_inside_one_confinement_layer(tmp_path):
    """検査は第三者のコードとモデルが書いたコードを実際に動かす。

    基盤のプロセスとして素で走らせると、作業場所の外も通信も届いてしまう。
    モデルのコマンドと同じプロファイルに入れる。終了コードは基盤が受け取るので、
    「通りました」という自己申告には依存しない。
    """
    workspace = app(tmp_path, tests=True)
    binaries = workspace / "frontend" / "node_modules" / ".bin"
    binaries.mkdir(parents=True)
    for name in ("vue-tsc", "vite"):
        (binaries / name).touch()
    (workspace / "frontend" / "tsconfig.json").write_text("{}", encoding="utf-8")
    (workspace / ".venv" / "bin").mkdir(parents=True)
    (workspace / ".venv" / "bin" / "pytest").touch()

    steps = verification_steps(workspace)
    assert [s.argv[0] for s in steps] == ["node_modules/.bin/vue-tsc", "node_modules/.bin/vite",
                                          ".venv/bin/pytest"]
    assert all(step.confined for step in steps)
    pytest_step = steps[-1]
    argv = argv_for(workspace, pytest_step)
    assert argv[0] == "bwrap" and "codex" not in argv
    # 二重bwrapを避け、1つのnetwork namespaceで外部通信を切る。
    assert "--unshare-net" in argv
    assert "--tmpfs" in argv
    assert pytest_step.timeout == PYTEST_TIMEOUT < VERIFY_TIMEOUT
    assert "-x" in pytest_step.argv


def test_nothing_is_verified_before_the_tools_exist(tmp_path):
    """導入が失敗した作業場所で検査を走らせると、道具が無いだけで「失敗」になる。"""
    assert verification_steps(app(tmp_path, tests=True)) == []


def test_an_app_without_tests_does_not_count_as_a_failed_test_run(tmp_path):
    """試験が1つも無いと pytest は終了コード5を返す。これを失敗として返さない。"""
    workspace = app(tmp_path)
    (workspace / ".venv" / "bin").mkdir(parents=True)
    (workspace / ".venv" / "bin" / "pytest").touch()
    assert verification_steps(workspace) == []


def test_the_failure_keeps_the_end_of_the_output(tmp_path):
    """要約すると、どのファイルの何行目かが消えて直せない。末尾を残す。"""
    step = Step("画面の型を検査しています。", ["vue-tsc", "--noEmit"], "frontend", True)
    detail = failure_detail(step, 2, "x" * 9000 + "\nsrc/app.vue(12,3): error TS2304")
    assert "src/app.vue(12,3): error TS2304" in detail
    assert "終了コード 2" in detail
    assert len(detail) < 5000


def test_the_declared_source_cannot_be_replaced_by_the_generated_app(monkeypatch):
    """取得元は基盤が決める。生成物や作業場所の環境では差し替えさせない。"""
    environment = toolchain_runner.install_environment(
        {"PATH": "/usr/bin", "UV_DEFAULT_INDEX": "https://evil.example/simple/",
         "AWS_SECRET_ACCESS_KEY": "should-not-be-passed"},
        {"UV_DEFAULT_INDEX": "https://pypi.example.com/simple/"})
    assert environment["UV_DEFAULT_INDEX"] == "https://pypi.example.com/simple/"
    # 取得に要らないものは渡さない。導入は第三者の tarball を展開する処理でもある。
    assert "AWS_SECRET_ACCESS_KEY" not in environment


def test_dependency_installers_keep_only_the_platform_ca_settings():
    """監査CAは必要だが、生成Podの資格情報全体を子プロセスへ渡してはいけない。"""
    bundle = "/run/audit-ca-bundle/ca-bundle.crt"
    environment = toolchain_runner.install_environment(
        {"PATH": "/usr/bin", "SSL_CERT_FILE": bundle,
         "NODE_EXTRA_CA_CERTS": bundle, "REQUESTS_CA_BUNDLE": bundle,
         "GOOGLE_APPLICATION_CREDENTIALS": "/run/vertex/credentials.json",
         "OPENAI_API_KEY": "secret"}, {})
    assert environment["SSL_CERT_FILE"] == bundle
    assert environment["NODE_EXTRA_CA_CERTS"] == bundle
    assert environment["REQUESTS_CA_BUNDLE"] == bundle
    assert "GOOGLE_APPLICATION_CREDENTIALS" not in environment
    assert "OPENAI_API_KEY" not in environment


def test_dependency_sandbox_can_read_the_platform_ca(tmp_path):
    """読取専用rootから、基盤のCA bundleを読めなければならない。"""
    from backend.domain.toolchain import argv_for

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    bundle = "/run/audit-ca-bundle/ca-bundle.crt"
    step = Step("取得", ["npm", "install"], "", True, True)
    argv = argv_for(workspace, step, readable=(bundle,))
    assert argv[0] == "bwrap"
    assert ["--ro-bind", "/", "/"] == argv[argv.index("--ro-bind"):argv.index("--ro-bind") + 3]
    assert "--unshare-net" not in argv


def test_a_step_that_cannot_start_is_reported_rather_than_raised(tmp_path):
    """道具が無い環境で例外にすると、生成そのものが落ちる。理由を返して続ける。"""
    step = Step("試しています。", ["koyorina-no-such-tool"], "", False)
    code, output = asyncio.run(toolchain_runner._run(tmp_path, step, {"PATH": "/nonexistent"}, 5))
    assert code == 127 and "koyorina-no-such-tool" in output


def test_package_caches_do_not_land_on_the_container_disk(tmp_path):
    """既定のキャッシュ置き場はHOME。生成PodのHOMEはコンテナのディスクで、
    ephemeral-storage の上限を依存の取得だけで超える。超えるとPodが追い出され、
    生成は理由の分からない中断に見える。共有PVC側の、作業場所の外へ逃がす。
    """
    cache = tmp_path / "cache"
    environment = toolchain_runner.install_environment({"HOME": "/home/forge"}, {}, cache)
    assert environment["UV_CACHE_DIR"] == str(cache / "uv")
    assert environment["NPM_CONFIG_CACHE"] == str(cache / "npm")
    assert cache.is_dir()
    # 作業場所の中に置くと、成果物と変更履歴に入ってしまう。
    assert "workspace" not in str(cache)


def test_what_was_installed_is_recorded(tmp_path):
    """通信の行き先は NetworkPolicy では残らない（落とすだけで記録しない）。

    行き先まで追うには CNI を替えるか取得元側のログを見ることになる。ここで
    残せるのは「結局どの版が入ったか」で、あとから追うときに要るのはこちら。
    """
    from backend.domain.toolchain import inventory_steps

    workspace = app(tmp_path)
    assert inventory_steps(workspace) == []

    (workspace / ".venv" / "bin").mkdir(parents=True)
    (workspace / ".venv" / "bin" / "python").touch()
    modules = workspace / "frontend" / "node_modules"
    modules.mkdir(parents=True)
    (modules / ".package-lock.json").write_text("{}", encoding="utf-8")

    steps = inventory_steps(workspace)
    assert [s.argv[:3] for s in steps] == [["uv", "pip", "freeze"],
                                           ["cat", "node_modules/.package-lock.json"]]
    # 記録も閉じ込めの中で取る。通信は要らない。
    assert all(step.confined and not step.network for step in steps)


def test_the_whole_output_is_captured_not_just_the_first_chunk(tmp_path):
    """StreamReader.read(n) は「nバイトまで」で、1バイトでも来れば返る。

    これで済ませると最初の1回ぶんしか取れない。サンドボックスが先に警告を
    1行出すので、実際に「警告だけが残り、本文が全部消える」形で出た。
    検査の失敗出力も同じ経路を通るため、修正ターンへ渡る内容も消えていた。
    """
    step = Step("出しています。",
                ["sh", "-c", "echo WARNING: first; sleep 0.2; for i in $(seq 1 3000); "
                             "do echo line-$i; done; echo LAST-LINE"], "", False)
    code, output = asyncio.run(toolchain_runner._run(tmp_path, step, dict(os.environ), 30))
    assert code == 0
    assert output.startswith("WARNING: first")
    assert "LAST-LINE" in output, "先頭の一塊しか読めていない"
    assert output.count("line-") == 3000


def test_output_beyond_the_cap_does_not_wedge_the_process(tmp_path):
    """上限で読むのをやめるとパイプが詰まり、子プロセスが終われなくなる。"""
    step = Step("大量に出しています。",
                ["sh", "-c", f"yes koyorina | head -c {toolchain_runner.MAX_CAPTURE * 3}"],
                "", False)
    code, output = asyncio.run(toolchain_runner._run(tmp_path, step, dict(os.environ), 30))
    assert code == 0
    assert len(output) <= toolchain_runner.MAX_CAPTURE


def test_installing_twice_does_not_fail_on_the_existing_venv(tmp_path):
    """2回目の導入は必ず既存の .venv に当たる。

    `uv venv` は既にあると失敗する。依存を足したときの入れ直しが毎回そこで
    落ち、「部品がそろっていないため、検査は行いません」を繰り返していた。
    モデルからは直しようがなく（規約で .venv を触るのを禁じている）、
    「Koyorina側で対処が必要」と言って止まる。
    """
    steps = install_steps(app(tmp_path))
    venv = next(s for s in steps if s.argv[:2] == ["uv", "venv"])
    assert "--allow-existing" in venv.argv


def test_a_frontend_that_cannot_be_built_is_reported_not_skipped(tmp_path):
    """道具が無いと検査は飛ぶ。飛ばしたことを黙っていると、通ったように見える。

    package.json に vite を書き忘れると node_modules/.bin/vite ができず、
    検査対象から外れる。「検査はすべて通りました」と出たうえで、プレビューの
    ビルドで落ちる。Geminiはコマンドを実行できないぶん、この書き忘れに
    自分で気づけない。
    """
    from backend.domain.toolchain import missing_tooling

    workspace = app(tmp_path)
    (workspace / "frontend" / "tsconfig.json").write_text("{}", encoding="utf-8")
    problems = missing_tooling(workspace)
    assert len(problems) == 2
    assert any("vite" in p and "devDependencies" in p for p in problems)
    assert any("プレビューはこのビルドを通らないと起動しません" in p for p in problems)

    binaries = workspace / "frontend" / "node_modules" / ".bin"
    binaries.mkdir(parents=True)
    (binaries / "vite").touch()
    (binaries / "vue-tsc").touch()
    assert missing_tooling(workspace) == []

    # 画面を持たないアプリでは何も言わない。
    assert missing_tooling(app(tmp_path, node=False, name="backend-only")) == []


def test_a_pod_that_cannot_sandbox_is_named_as_such(tmp_path):
    """何もしないコマンドが落ちたなら、原因は中身ではなく実行環境。

    見分けないと、モデルへ「直せ」と言って戻すことになる。直せない。
    seccompまたはmount設定が不完全なら、bwrapを起動できない。
    """
    from backend.domain.toolchain import SANDBOX_PROBE, sandbox_unavailable

    assert SANDBOX_PROBE.confined and not SANDBOX_PROBE.network
    assert sandbox_unavailable(0, "") == ""
    message = sandbox_unavailable(1, "bwrap: Operation not permitted")
    assert "アプリの不具合ではありません" in message
    # 出力から拾った手掛かりを添える。何も言わないと調べようがない。
    assert "Operation not permitted" in message and "seccomp" in message
    # 手掛かりが無くても、環境の問題だとは言い切る。
    assert "検査を実行できません" in sandbox_unavailable(127, "")
