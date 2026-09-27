"""送る側のパスが、受ける側に実在するか。

Koyorina は3段の中継でできている。

    管理アプリ → 生成コントローラ → 生成エージェント
    管理アプリ → プレビューコントローラ

途中の1段に口が無くても、誰も気づかない。呼び出し側は 404 を受け取り、
それを「見つかりません」という利用者向けの文言へ変えてしまうため、
画面には「まだ記録がありません」のように、空っぽと同じ顔で出る。

実際に変更履歴がそうなっていた。画面もエージェントも作ってあったのに、
コントローラに中継が無く、保存されているのに一度も表示できていなかった。
テストが controller を差し替えていたので、どのテストも踏まなかった。

ここでは本物のルーティング表と突き合わせる。
"""
import re
from pathlib import Path
import pytest
from starlette.routing import Match

ROOT = Path(__file__).resolve().parents[2]
SAMPLE = "11111111-1111-4111-8111-111111111111"
# f文字列の中身は実行しないと決まらない。値が限られているものだけ、ここで展開する。
# 増えたら足す。足し忘れても「合わない」として落ちるので、黙って見逃さない。
PLACEHOLDERS = {
    "{action}": ("login", "logout", "answer", "cancel"),
    "{query}": ("",),
    "{suffix}": ("",),
}


def routed(app, method: str, path: str) -> bool:
    scope = {"type": "http", "method": method, "path": path, "path_params": {},
             "headers": [], "query_string": b"", "root_path": ""}
    return any(route.matches(scope)[0] == Match.FULL for route in app.router.routes)


def candidates(path: str):
    """1つの呼び出しから、実際に飛びうるパスを組み立てる。"""
    path = path.split("?")[0]
    variants = [path]
    for token, values in PLACEHOLDERS.items():
        if any(token in variant for variant in variants):
            variants = [variant.replace(token, value) for variant in variants for value in values]
    # 残りは識別子。UUIDでも通る形になっていること。
    return [re.sub(r"\{[^}]+\}", SAMPLE, variant) for variant in variants]


def calls(paths, pattern: str):
    found = []
    for path in paths:
        text = path.read_text(encoding="utf-8")
        for method, target in re.findall(pattern, text, re.S):
            found.append((path.name, method, target))
    return sorted(set(found))


def codex_controller():
    from backend.worker.controller import ControllerSettings, create_controller
    return create_controller(ControllerSettings(
        token="x" * 40,
        agent_image="registry.example.com/koyorina-agent@sha256:" + "a" * 64))


def preview_controller():
    from backend.worker.preview_controller import ControllerSettings, create_controller
    return create_controller(ControllerSettings(
        token="x" * 40,
        runtime_image="registry.example.com/koyorina-preview-runtime@sha256:" + "a" * 64))


def agent_app():
    from backend.worker.agent import AgentSettings, create_agent
    return create_agent(AgentSettings(token="x" * 40, user_id=SAMPLE))


def check(app, found, prefix, minimum):
    # 読み取れた数も見る。書き方が変わって正規表現が空振りしたら、
    # 「全部OK」と嘘をつくことになる。
    assert len(found) >= minimum, f"呼び出しを{minimum}件以上読めるはず（実際 {len(found)}）"
    missing = [(name, method, target) for name, method, target in found
               if not any(routed(app, method, prefix + option) for option in candidates(target))]
    assert not missing, "受ける側に口が無い: " + ", ".join(
        f"{method} {target}（{name}）" for name, method, target in missing)


def test_the_management_app_only_asks_for_paths_the_codex_controller_serves():
    sources = sorted((ROOT / "backend" / "api").glob("*.py")) + \
              sorted((ROOT / "backend" / "core").glob("*.py"))
    found = calls(sources, r'controller\(\s*[^,]+,\s*[^,]+,\s*"(GET|POST|PUT|DELETE|PATCH)",'
                           r'\s*\n?\s*f?"([^"]+)"')
    auth = [item for item in found if item[2].split("?")[0] in
                {"/account", "/models", "/login", "/logout", "/runtimes"}]
    tenant = [item for item in found if item not in auth]
    check(codex_controller(), auth, "/users/" + SAMPLE, minimum=4)
    check(codex_controller(), tenant,
          "/tenants/" + SAMPLE + "/users/" + SAMPLE, minimum=16)


def test_the_management_app_only_asks_for_paths_the_preview_controller_serves():
    source = [ROOT / "backend" / "core" / "preview_backend.py"]
    found = calls(source, r'self\.call\(\s*"(GET|POST|PUT|DELETE)",\s*project_id,\s*tenant_id,?\s*(?:"([^"]*)")?')
    check(preview_controller(), found,
          "/tenants/" + SAMPLE + "/projects/" + SAMPLE, minimum=5)


def test_the_codex_controller_only_asks_for_paths_the_agent_serves():
    source = [ROOT / "backend" / "worker" / "controller.py"]
    found = calls(source, r'relay\(\s*[^,]+,\s*"(GET|POST|PUT|DELETE)",\s*\n?\s*f?"([^"]+)"')
    check(agent_app(), found, "", minimum=18)


@pytest.mark.parametrize("path", ["/projects/{project_id}/history",
                                  "/projects/{project_id}/history/{commit}"])
def test_the_change_history_is_reachable_end_to_end(path):
    """今回抜けていた経路。3段とも口があること。"""
    url = re.sub(r"\{[^}]+\}", SAMPLE, path)
    assert routed(codex_controller(), "GET",
                  "/tenants/" + SAMPLE + "/users/" + SAMPLE + url)
    assert routed(agent_app(), "GET", url)
