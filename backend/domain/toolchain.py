"""宣言どおりの依存を実際に入れて、実際に動かして確かめる。

## なぜモデルにやらせず、基盤が走らせるのか

ローカルPCでCodexを使うときは、モデル自身が `npm install` を走らせる。ここでは
そうしない。モデルのターンで通信を許すと、そのターンに打つ**すべての**コマンドが
通信できるようになる（通信の可否はターン単位の設定で、コマンド単位に絞れない）。
基盤が走らせれば、通信を使う瞬間を導入の1ステップに限定でき、取得元も固定できる。

    npm は --ignore-scripts        postinstall を走らせない
    uv は --only-binary=:all:      sdist の setup.py を走らせない

導入の時点では第三者のコードを1行も実行しない。ホイールと tarball を展開するだけ。

## なぜ導入も検査もサンドボックスの中で走らせるのか

生成Podは `/data/projects` を丸ごとマウントしている。Podは利用者ごとで、その人の
全アプリの依頼を受け持つので、マウントを1アプリに絞ることはできない。素のプロセス
として導入すると、他のアプリの作業場所にも手が届く位置で動くことになる。

そこで導入も検査も基盤が直接 `bwrap` の中で走らせる。rootは読み取り専用にし、
`/data`、資格情報、ホームを空の領域で覆って、対象アプリと取得キャッシュだけを
戻す。触れるのはそのアプリの作業場所だけになる。違うのは通信だけ。

                  触れる範囲            通信
    導入          このアプリの作業場所   あり（取得元は基盤が固定）
    検査          このアプリの作業場所   なし
    モデルのターン このアプリの作業場所   なし

検査を基盤が走らせるのには、もうひとつ理由がある。`pytest` も `vite build` も
実際にコードを動かすので、終了コードを基盤が直接受け取る形にしておかないと、
モデルの「通りました」を証拠として扱うことになってしまう。
"""
from pathlib import Path
from typing import NamedTuple

FRONTEND = "frontend"
VENV = ".venv"
# 導入は取得と展開だけだが、依存が多いと分を要する。検査は1回あたりはもっと短い。
INSTALL_TIMEOUT = 900
VERIFY_TIMEOUT = 180
PYTEST_TIMEOUT = 120
SENSITIVE_ROOTS = ("/proc", "/tmp", "/home", "/root", "/app", "/run/vertex", "/data")


class Step(NamedTuple):
    label: str          # 画面へ出す日本語。利用者はコマンドを読めない。
    argv: list[str]
    cwd: str            # 作業場所からの相対。"" は作業場所そのもの。
    confined: bool      # True ならサンドボックスの中で走らせる。
    network: bool = False  # 通信を許すのは取得のときだけ。
    timeout: int = VERIFY_TIMEOUT


def install_steps(workspace: Path) -> list[Step]:
    """宣言があるものだけ入れる。無い側は飛ばす（片方だけのアプリもある）。"""
    steps = []
    if (workspace / "pyproject.toml").is_file():
        steps.append(Step("Pythonの部品をそろえています。",
                          # 既にあるときは作り直さない。--allow-existing が無いと、
                          # 2回目の導入（依存を足したとき）が必ずここで落ちる。
                          ["uv", "venv", "--quiet", "--allow-existing", VENV], "", True))
        steps.append(Step("Pythonの部品を取得しています。",
                          ["uv", "pip", "install", "--quiet", "--python", f"{VENV}/bin/python",
                           # ソース配布物のビルドは setup.py の実行になる。入れない。
                           "--only-binary", ":all:", "-r", "pyproject.toml"], "", True, True))
    if (workspace / FRONTEND / "package.json").is_file():
        steps.append(Step("画面の部品を取得しています。",
                          ["npm", "install", "--ignore-scripts", "--no-package-lock",
                           # package-lock.json は成果物として受け取らない決まりなので作らせない。
                           "--no-audit", "--no-fund", "--loglevel", "error"], FRONTEND, True, True))
    return steps


def verification_steps(workspace: Path) -> list[Step]:
    """入ったものを使って、本当に通るかを見る。道具がある場合だけ走らせる。"""
    steps = []
    frontend = workspace / FRONTEND
    binaries = frontend / "node_modules" / ".bin"
    # tsconfigがないプロジェクトでvue-tscを起動すると、型エラーではなく
    # TypeScriptのヘルプを表示して終了する。生成時の契約と同じ条件で実行する。
    if ((binaries / "vue-tsc").exists()
            and ((frontend / "tsconfig.json").is_file()
                 or (frontend / "tsconfig.app.json").is_file())):
        steps.append(Step("画面の型を検査しています。",
                          ["node_modules/.bin/vue-tsc", "--noEmit"], FRONTEND, True))
    if (binaries / "vite").exists():
        steps.append(Step("画面を組み立てて確かめています。",
                          ["node_modules/.bin/vite", "build"], FRONTEND, True))
    if (workspace / VENV / "bin" / "pytest").exists() and _has_tests(workspace):
        steps.append(Step("アプリの試験を実行しています。",
                          [f"{VENV}/bin/pytest", "-q", "-x", "--no-header",
                           "-p", "no:cacheprovider"],
                          "", True, False, PYTEST_TIMEOUT))
    return steps


# サンドボックスが使えるかを見るだけの、何もしないコマンド。
SANDBOX_PROBE = Step(
    "実行環境を確かめています。",
    ["python", "-c", "import socket; a,b=socket.socketpair(); a.close(); b.close()"],
    "", True, False, 15)
SANDBOX_PROBE_TIMEOUT = 60
# 閉じ込めが用意できていないPodで出る形。値は環境によって違うので、広めに見る。
SANDBOX_DENIED = ("Operation not permitted", "Permission denied", "bwrap",
                  "seccomp", "clone", "unshare", "No such file or directory")


def sandbox_unavailable(returncode: int, output: str) -> str:
    """閉じ込めそのものが使えないのか、コマンドが失敗しただけかを見分ける。

    何もしないコマンドが落ちたなら、原因は中身ではなく実行環境。ここを
    見分けないと、モデルへ「直せ」と言って戻すことになる（直せない）。
    Podのseccompまたはmount設定が不完全なら、bwrapを起動できない。
    """
    if not returncode:
        return ""
    hint = next((word for word in SANDBOX_DENIED if word in output), "")
    return ("この生成Podでは検査を実行できません（閉じ込めの仕組みを起動できない）。"
            + (f"手掛かり: {hint}。" if hint else "")
            + "アプリの不具合ではありません。管理者にPodのseccompプロファイルの"
              "確認を依頼してください。")


def missing_tooling(workspace: Path) -> list[str]:
    """宣言はあるのに、検査に要る道具が入っていない状態を見つける。

    道具が無いと verification_steps はその検査を飛ばす。飛ばしたことを黙って
    いると「検査はすべて通りました」と出たうえで、プレビューのビルドで落ちる。
    走らせられなかったことは、通ったことと違う。
    """
    problems = []
    frontend = workspace / FRONTEND
    if (frontend / "package.json").is_file():
        binaries = frontend / "node_modules" / ".bin"
        tools = [("vite", "画面を組み立てられません")]
        if ((frontend / "tsconfig.json").is_file()
                or (frontend / "tsconfig.app.json").is_file()):
            tools.append(("vue-tsc", "画面の型を検査できません"))
        for tool, reason in tools:
            if not (binaries / tool).exists():
                problems.append(
                    f"`{tool}` が frontend/node_modules にありません。{reason}。\n"
                    f"frontend/package.json の devDependencies に `{tool}` を加えてください"
                    "（Koyorina が導入し直します）。プレビューはこのビルドを通らないと起動しません。")
    return problems


def _has_tests(workspace: Path) -> bool:
    """試験が1つも無いのに pytest を走らせると、終了コード5で「失敗」に見える。"""
    for path in workspace.rglob("test_*.py"):
        if not any(part in {VENV, "node_modules", ".git"} for part in path.relative_to(workspace).parts):
            return True
    return False


def inventory_steps(workspace: Path) -> list[Step]:
    """何が入ったかを記録する。通信の監査ではないが、実際に問えるのはこちら。

    NetworkPolicy は通信を落とせても記録は残さない。行き先まで残すには CNI を
    替えるか、取得元のログ（Takumi Guard 側）を見ることになる。ここで残すのは
    「結局どの版が入ったか」で、あとから追うときに実際に必要になるのはこれ。
    """
    steps = []
    if (workspace / VENV / "bin" / "python").exists():
        steps.append(Step("Pythonの部品の一覧を記録しています。",
                          ["uv", "pip", "freeze", "--python", f"{VENV}/bin/python"], "", True))
    if (workspace / FRONTEND / "node_modules" / ".package-lock.json").is_file():
        # npm が node_modules の中へ必ず書く解決結果。版と integrity が入っている。
        steps.append(Step("画面の部品の一覧を記録しています。",
                          ["cat", "node_modules/.package-lock.json"], FRONTEND, True))
    return steps


def argv_for(workspace: Path, step: Step, writable=(), readable=()) -> list[str]:
    """実際に起動する形。閉じ込めが要るものはbubblewrapへ直接渡す。

    writable は作業場所の外で書かせたい場所（取得キャッシュ）。名指しで足す。

    Codex sandboxの内側でさらにbwrapを起動すると、コンテナ内では外側bwrapの
    procfs再マウントがEPERMになる。検査と導入は基盤が決めた固定コマンドなので、
    ここではbwrapを1回だけ起動する。/dataを空にして対象workspaceとcacheだけを
    戻し、Vertex資格情報、Codex認証、他プロジェクトを生成コードから隠す。
    """
    directory = workspace / step.cwd if step.cwd else workspace
    if not step.confined:
        return list(step.argv)
    workspace = workspace.resolve(strict=True)
    selected = [(workspace, "--bind")]
    selected.extend((Path(item).resolve(), "--bind") for item in writable if item)
    selected.extend((Path(item).resolve(), "--ro-bind") for item in readable if item)

    # rootは道具とライブラリのため読取専用で見せる。秘密や別アプリがある場所は
    # 空のtmpfsで覆い、必要な対象だけ後からbindし直す。
    hidden = [Path(path) for path in SENSITIVE_ROOTS if Path(path).exists()]
    argv = ["bwrap", "--die-with-parent", "--new-session",
            "--ro-bind", "/", "/", "--dev-bind", "/dev", "/dev"]
    for path in hidden:
        argv.extend(("--tmpfs", str(path)))

    made = set(hidden)
    for target, mode in selected:
        for root in hidden:
            if target != root and target.is_relative_to(root):
                parents = []
                parent = target.parent
                while parent != root:
                    parents.append(parent)
                    parent = parent.parent
                for item in reversed(parents):
                    if item not in made:
                        argv.extend(("--dir", str(item)))
                        made.add(item)
                break
        argv.extend((mode, str(target), str(target)))
    if not step.network:
        argv.append("--unshare-net")
    return [*argv, "--chdir", str(directory), "--", *step.argv]


# モデルへ渡す出力の上限。長すぎると依頼そのものが通らなくなる。
MAX_OUTPUT = 4000


def failure_detail(step: Step, returncode: int, output: str) -> str:
    """モデルへ渡す形。要約しない。

    原因を1行に丸めると直せなくなる（どのファイルの何行目か、が消える）。
    長い出力は先頭ではなく末尾を残す。失敗の理由は末尾に出る。
    """
    text = output.strip()
    if len(text) > MAX_OUTPUT:
        text = "…（省略）\n" + text[-MAX_OUTPUT:]
    return f"$ {' '.join(step.argv)}\n(終了コード {returncode})\n{text}"


def failure_summary(step: Step) -> str:
    """画面へ出す形。利用者はコマンドも英語のログも読まない。"""
    return step.label.rstrip("。") + "が通りませんでした。"
