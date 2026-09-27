"""Credential-free native smoke test for Koyorina's Codex permission profile."""
import os
from pathlib import Path
import subprocess
import tempfile

# 本番と同じプロファイルを検査する。ここで組み直すと、実装が変わっても気づけない。
from backend.domain.generation import generation_permission_args


def main():
    data_root = os.environ.get("AGENT_ROOT", "/data")
    user_id = os.environ.get("AGENT_USER_ID")
    # 本物の保存領域の上で確かめる。コンテナの /tmp とはファイルシステムが違い、
    # サンドボックスの挙動もそこに依存しうるため。
    # 利用者ごとのPVCの下へ置く。ジョブは共有PVC側（/data/jobs）にあって
    # /data/<利用者>/jobs は存在しない。指したまま実行すると、そこで落ちる。
    smoke_parent = Path(data_root) / user_id / "smoke" if data_root and user_id else None
    if smoke_parent is not None:
        try:
            smoke_parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        except OSError as exc:
            print(f"作業領域を用意できないため、既定の一時領域で確かめます: {exc}")
            smoke_parent = None
    with tempfile.TemporaryDirectory(prefix="forge-workspace-", dir=smoke_parent) as directory:
        root = Path(directory)
        workspace = root / "workspace"
        workspace.mkdir()
        outside = root / "outside-canary.txt"
        outside.write_text("NON_SECRET_CANARY")
        environment = {key: os.environ[key] for key in ("PATH", "LANG", "TMPDIR") if key in os.environ}
        environment.update({"HOME": str(root), "CODEX_HOME": str(root / "codex")})
        (root / "codex").mkdir()

        def run(command):
            return subprocess.run(
                ["codex", "sandbox", *generation_permission_args(workspace), "-P", "app_forge_job",
                 "-C", str(workspace), *command],
                cwd=workspace, env=environment, capture_output=True, text=True, timeout=10)

        allowed = run(["sh", "-c", "printf ok > allowed.txt"])
        assert allowed.returncode == 0 and (workspace / "allowed.txt").read_text() == "ok"
        denied_read = run(["sh", "-c", "cat ../outside-canary.txt"])
        assert denied_read.returncode != 0 and "NON_SECRET_CANARY" not in denied_read.stdout
        # The shell can report success for an overlay-only parent write, but it must not materialize
        # outside the workspace. Koyorina also keeps status/auth data in a separate directory.
        run(["sh", "-c", "printf no > ../buffer.txt"])
        assert not (root / "buffer.txt").exists()
        escape_name = f"escape-{root.name}.txt"
        run(["sh", "-c", f"printf no > ../../{escape_name}"])
        assert not (root.parent / escape_name).exists()
        denied_network = run(["python3", "-c",
            "import socket; socket.create_connection(('1.1.1.1', 443), 2)"])
        assert denied_network.returncode != 0
        print("workspace profile: inside write persisted; outside reads, writes and direct network denied")

        # 依存の導入は同じ閉じ込めで、通信だけを許して走らせる。ここが崩れると、
        # 「取得できない」か「他のアプリの作業場所に手が届く」のどちらかになる。
        cache = root / "cache"
        cache.mkdir()
        other = root / "other-app"
        other.mkdir()
        (other / "canary.txt").write_text("NON_SECRET_OTHER_APP")

        def run_install(command):
            return subprocess.run(
                ["codex", "sandbox",
                 *generation_permission_args(workspace, network=True, writable=(cache,)),
                 "-P", "app_forge_job", "-C", str(workspace), *command],
                cwd=workspace, env=environment, capture_output=True, text=True, timeout=30)

        # 通信が本当に開くこと。開かないと導入が常に失敗する（フラグの綴り違いなど）。
        allowed_network = run_install(["python3", "-c",
            "import socket; socket.create_connection(('1.1.1.1', 443), 5).close()"])
        assert allowed_network.returncode == 0, allowed_network.stderr[:400]
        # 通信を許しても、触れる範囲は広がらないこと。
        other_app = run_install(["sh", "-c", f"cat {other}/canary.txt"])
        assert other_app.returncode != 0 and "NON_SECRET_OTHER_APP" not in other_app.stdout
        # 取得キャッシュは名指しで許した場所だけ。
        assert run_install(["sh", "-c", f"printf ok > {cache}/probe"]).returncode == 0
        assert (cache / "probe").read_text() == "ok"
        print("install profile: network allowed; other application workspaces still denied")

        # スキルは作業場所の中（.agents/skills）。追加の許可なしで読めること、
        # かつ資産を持ち出せることを見る。元は CODEX_HOME に置いていて、
        # サンドボックスの外だったため cp は必ず Permission denied になっていた。
        skills = workspace / ".agents" / "skills" / "example-skill"
        skills.mkdir(parents=True)
        (skills / "asset.txt").write_text("SKILL_ASSET")
        copied = run(["sh", "-c",
                      "cp .agents/skills/example-skill/asset.txt ./from-skill.txt"])
        assert copied.returncode == 0, copied.stderr[:400]
        assert (workspace / "from-skill.txt").read_text() == "SKILL_ASSET"
        # 認証のある場所は、これまでどおり読めない。
        assert run_install(["sh", "-c", f"cat {other}/canary.txt"]).returncode != 0
        print("skills: readable inside the workspace with no extra grant; neighbours still denied")


if __name__ == "__main__":
    main()
