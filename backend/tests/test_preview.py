"""生成コードの展開とプレビュー操作。Dockerは呼ばず、実行基盤は差し替える。"""
import json
import httpx
import pytest
from sqlalchemy import select
from backend.core.db import Audit, GenerationJob, Project, ProjectCollaborator, Tenant, User, UserTenant
from backend.domain.generation import CodeBundle
from backend.domain.preview import (PreviewPaths, allocate_port, cookie_prefix, dependency_digest,
                                    forward_secret, identity_headers, materialize, read_state)
from backend.tests.test_api import context, login, INPUT
from backend.tests.test_codex_generation import BUNDLE


@pytest.fixture
def preview(context, tmp_path, monkeypatch):
    client, sessions = context
    settings = client.app.state.settings
    client.app.state.settings = settings.model_copy(update={
        "preview_enabled": True, "preview_root": tmp_path / "previews",
        # 既定では閉じている口。ここでは機能そのものを試すので開ける。
        "preview_shell_enabled": True, "local_codex_enabled": True,
        "preview_port_base": 8101, "preview_port_count": 2})
    calls = []

    async def run(project_id, paths, port, image, environment):
        calls.append(("run", str(project_id), port, environment))

    async def remove(project_id):
        calls.append(("remove", str(project_id)))

    async def container_state(project_id):
        return state["container"]

    async def responding(port):
        return state["responding"]

    async def logs(project_id, lines=200):
        return "[preview] 起動しました。"

    state = {"container": "running", "responding": True}
    for name, value in (("run", run), ("remove", remove), ("container_state", container_state),
                        ("responding", responding), ("logs", logs)):
        monkeypatch.setattr("backend.core.preview_runtime." + name, value)
    return client, sessions, calls, state


def generated_job(sessions, client, owner="alice@example.com"):
    if owner != "alice@example.com":
        with sessions.begin() as db:
            member = db.query(User).filter_by(email=owner).one()
            tenant_id = db.scalar(select(Tenant.id))
            if tenant_id and not db.get(UserTenant, (member.id, tenant_id)):
                db.add(UserTenant(user_id=member.id, tenant_id=tenant_id, role="developer"))
    login(client, owner)
    project = client.post("/api/projects", json=INPUT).json()
    client.post(f"/api/projects/{project['id']}/approve", json={"revision": 1})
    with sessions.begin() as db:
        record = db.get(Project, project["id"])
        job = GenerationJob(project_id=record.id, owner_id=record.owner_id, revision=1,
                            specification=INPUT, source_type="local_codex", artifact=BUNDLE,
                            status="generated")
        db.add(job)
        db.flush()
        return project["id"], job.id


def test_materialize_keeps_dependencies_and_removes_stale_sources(tmp_path):
    workspace = tmp_path / "workspace"
    (workspace / "backend").mkdir(parents=True)
    (workspace / "backend" / "old.py").write_text("x = 1\n")
    (workspace / "frontend" / "node_modules" / "vite").mkdir(parents=True)
    (workspace / "frontend" / "node_modules" / "vite" / "index.js").write_text("//")
    digest = materialize(workspace, CodeBundle.model_validate(BUNDLE))
    assert not (workspace / "backend" / "old.py").exists()
    assert (workspace / "backend" / "main.py").read_text().startswith("from fastapi")
    assert (workspace / "frontend" / "node_modules" / "vite" / "index.js").is_file()
    assert digest == materialize(workspace, CodeBundle.model_validate(BUNDLE))
    assert dependency_digest(workspace) == dependency_digest(workspace)


def test_port_allocation_reuses_and_exhausts(tmp_path):
    root = tmp_path / "previews"
    first, second = "11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222"
    third = "33333333-3333-4333-8333-333333333333"
    for project_id in (first, second):
        paths = PreviewPaths(root, project_id).prepare()
        port = allocate_port(root, project_id, 8101, 2)
        (paths.state_file).write_text(json.dumps({"port": port}))
    assert allocate_port(root, first, 8101, 2) == 8101
    assert allocate_port(root, second, 8101, 2) == 8102
    with pytest.raises(ValueError):
        allocate_port(root, third, 8101, 2)


def test_disabled_preview_rejects_actions(context):
    client, sessions = context
    project_id, job_id = generated_job(sessions, client)
    assert client.get(f"/api/projects/{project_id}/preview").json() == {
        "enabled": False, "state": "stopped", "url": None, "port": None, "job_id": None,
        "updated_at": None, "message": "プレビューは無効です。", "hint": None, "evidence": None}
    assert client.post(f"/api/projects/{project_id}/jobs/{job_id}/preview", json={}).status_code == 503
    assert client.request("DELETE", f"/api/projects/{project_id}/preview", json={}).status_code == 503


def test_start_materializes_runs_and_reports_only_live_state(preview):
    client, sessions, calls, state = preview
    project_id, job_id = generated_job(sessions, client)
    result = client.post(f"/api/projects/{project_id}/jobs/{job_id}/preview", json={})
    assert result.status_code == 202, result.text
    body = result.json()
    assert body["state"] == "running" and body["url"] == f"/apps/{project_id}/" and body["job_id"] == job_id
    paths = PreviewPaths(client.app.state.settings.preview_root, project_id)
    assert (paths.workspace / "backend" / "main.py").is_file()
    assert read_state(paths)["port"] == 8101
    environment = [call for call in calls if call[0] == "run"][0][3]
    # ブラウザから見えるオリジンはKoyorina自身。Googleの生成元登録を増やさない。
    assert environment["APP_ORIGIN"] == "https://forge.test"
    assert environment["APP_BASE_PATH"] == f"/apps/{project_id}/"
    assert environment["DATABASE_URL"].startswith("sqlite") and "forge" not in environment["DATABASE_URL"]
    with sessions() as db:
        assert db.query(Audit).filter(Audit.action == "preview.started",
                                     Audit.resource_id == job_id).count() == 1
    # コンテナが落ちたら稼働中と表示しない。
    state["container"] = "exited"
    assert client.get(f"/api/projects/{project_id}/preview").json()["state"] == "failed"
    state["container"], state["responding"] = "running", False
    assert client.get(f"/api/projects/{project_id}/preview").json()["state"] == "starting"
    state["container"] = "none"
    assert client.get(f"/api/projects/{project_id}/preview").json()["state"] == "stopped"


def test_restart_requires_materialized_code_and_stop_removes_container(preview):
    client, sessions, calls, state = preview
    project_id, job_id = generated_job(sessions, client)
    assert client.post(f"/api/projects/{project_id}/preview/restart", json={}).status_code == 409
    client.post(f"/api/projects/{project_id}/jobs/{job_id}/preview", json={})
    assert client.post(f"/api/projects/{project_id}/preview/restart", json={}).status_code == 202
    assert client.get(f"/api/projects/{project_id}/preview/logs").json()["logs"].startswith("[preview]")
    client.request("DELETE", f"/api/projects/{project_id}/preview", json={})
    assert ("remove", project_id) in calls


def test_owner_isolation(preview):
    client, sessions, calls, state = preview
    project_id, job_id = generated_job(sessions, client)
    login(client, "bob@example.com")
    assert client.get(f"/api/projects/{project_id}/preview").status_code == 404
    assert client.post(f"/api/projects/{project_id}/jobs/{job_id}/preview", json={}).status_code == 404
    assert client.post(f"/api/projects/{project_id}/preview/restart", json={}).status_code == 404
    assert client.request("DELETE", f"/api/projects/{project_id}/preview", json={}).status_code == 404
    assert client.get(f"/api/projects/{project_id}/preview/logs").status_code == 404
    assert not [call for call in calls if call[0] == "run"]


def test_discard_removes_workspace(preview):
    client, sessions, calls, state = preview
    project_id, job_id = generated_job(sessions, client)
    client.post(f"/api/projects/{project_id}/jobs/{job_id}/preview", json={})
    paths = PreviewPaths(client.app.state.settings.preview_root, project_id)
    assert paths.base.is_dir()
    assert client.request("DELETE", f"/api/projects/{project_id}/preview/workspace", json={}).status_code == 200
    assert not paths.base.exists()


class FakeResponse:
    def __init__(self, content=b"ok", status_code=200, headers=None):
        self.content, self.status_code = content, status_code
        self.headers = httpx.Headers(headers or [("content-type", "text/plain")])


@pytest.fixture
def proxy(preview, monkeypatch):
    client, sessions, calls, state = preview
    sent = {}

    async def upstream(method, target, headers, body):
        sent.update({"method": method, "target": target, "headers": headers, "body": body})
        response = state.get("response") or FakeResponse()
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr("backend.api.app_proxy.upstream", upstream)
    return client, sessions, calls, state, sent


def started(client, sessions):
    project_id, job_id = generated_job(sessions, client)
    client.post(f"/api/projects/{project_id}/jobs/{job_id}/preview", json={})
    return project_id


def test_proxy_requires_login_and_project_access(proxy):
    client, sessions, calls, state, sent = proxy
    project_id = started(client, sessions)
    client.post("/auth/logout", json={})
    assert client.get(f"/apps/{project_id}/").status_code == 401
    login(client, "bob@example.com")
    assert client.get(f"/apps/{project_id}/").status_code == 404
    assert not sent


def test_collaborator_can_open_preview_but_loses_access_when_unshared(proxy):
    client, sessions, calls, state, sent = proxy
    project_id = started(client, sessions)
    with sessions.begin() as db:
        project = db.get(Project, project_id)
        owner = db.get(User, project.owner_id)
        collaborator = db.scalar(select(User).where(User.email == "bob@example.com"))
        if not db.get(UserTenant, (collaborator.id, project.tenant_id)):
            db.add(UserTenant(user_id=collaborator.id, tenant_id=project.tenant_id,
                              role="developer"))
        db.add(ProjectCollaborator(project_id=project.id, user_id=collaborator.id,
                                   added_by=owner.id))
    login(client, "bob@example.com")
    entry = client.get(f"/apps/{project_id}", follow_redirects=False)
    assert entry.status_code == 307
    assert entry.headers["location"] == f"/apps/{project_id}/"
    page = client.get(f"/apps/{project_id}/")
    assert page.status_code == 200
    assert sent["target"].endswith("/")
    asset = client.get(f"/apps/{project_id}/index.html")
    assert asset.status_code == 200
    assert sent["headers"]["X-Forge-User-Email"] == "bob@example.com"
    assert sent["headers"]["X-Forge-User-Admin"] == "false"

    with sessions.begin() as db:
        db.delete(db.get(UserTenant, (collaborator.id, project.tenant_id)))
    assert client.get(f"/apps/{project_id}/").status_code == 404

    with sessions.begin() as db:
        db.add(UserTenant(user_id=collaborator.id, tenant_id=project.tenant_id,
                          role="developer"))
        db.delete(db.get(ProjectCollaborator, (project_id, collaborator.id)))
    assert client.get(f"/apps/{project_id}/").status_code == 404
    assert client.get(f"/apps/{project_id}/index.html").status_code == 404


def test_proxy_hides_app_forge_session_and_scopes_cookies(proxy):
    client, sessions, calls, state, sent = proxy
    project_id = started(client, sessions)
    prefix = cookie_prefix(project_id)
    state["response"] = FakeResponse(headers=[
        ("content-type", "text/html"),
        ("set-cookie", "__Host-receipt_session=v1; Path=/; HttpOnly; Secure; SameSite=Lax"),
        ("x-frame-options", "DENY"),
        ("content-security-policy", "default-src 'self'; frame-ancestors 'none'"),
    ])
    client.cookies.set(prefix + "receipt_session", "v1")
    client.cookies.set("othercookie", "keep")
    result = client.get(f"/apps/{project_id}/index.html")
    assert result.status_code == 200
    # Koyorinaのセッションは生成アプリへ渡さない。自分のCookieだけ元の名前へ戻す。
    forwarded = sent["headers"]["Cookie"]
    assert "receipt_session=v1" in forwarded and "othercookie=keep" in forwarded
    assert "session=" not in forwarded.replace("receipt_session=", "")
    assert sent["target"].endswith("/index.html")
    cookie = result.headers["set-cookie"]
    assert cookie.startswith(prefix + "receipt_session=v1")
    assert f"Path=/apps/{project_id}/" in cookie
    # Koyorinaの画面に埋め込めるようにする。アプリ自身のCSPは残す。
    assert "x-frame-options" not in result.headers
    assert result.headers["content-security-policy"] == "default-src 'self'; frame-ancestors 'self'"


def test_proxy_rewrites_redirects_and_reports_stopped_app(proxy):
    client, sessions, calls, state, sent = proxy
    project_id = started(client, sessions)
    state["response"] = FakeResponse(status_code=302, headers=[("location", "/login")])
    result = client.get(f"/apps/{project_id}/records", follow_redirects=False)
    assert result.headers["location"] == f"/apps/{project_id}/login"
    state["response"] = httpx.ConnectError("refused")
    assert client.get(f"/apps/{project_id}/").status_code == 409


def test_app_forge_pages_keep_their_own_headers(proxy):
    client, sessions, calls, state, sent = proxy
    project_id = started(client, sessions)
    forge = client.get("/api/projects")
    assert "frame-ancestors 'none'" in forge.headers["content-security-policy"]
    assert forge.headers["x-frame-options"] == "DENY"
    assert client.get(f"/apps/{project_id}/").headers.get("x-frame-options") is None


def test_forwarded_identity_is_injected_and_client_headers_are_dropped(proxy):
    client, sessions, calls, state, sent = proxy
    project_id = started(client, sessions)
    result = client.get(f"/apps/{project_id}/api/session",
                        headers={"X-Forge-User-Email": "attacker@example.com",
                                 "X-Forge-Auth": "guessed", "X-Forge-User-Admin": "true",
                                 "X-CSRF-Token": "app-token"})
    assert result.status_code == 200
    headers = sent["headers"]
    # 利用者が送った本人情報は捨て、Koyorinaが確認した本人だけを渡す。
    assert headers["X-Forge-User-Email"] == "alice@example.com"
    # アプリ独自のヘッダー（CSRFトークンなど）は落とさない。
    assert headers["x-csrf-token"] == "app-token"
    assert headers["X-Forge-User-Admin"] == "true"
    settings = client.app.state.settings
    secret = forward_secret(project_id, settings.app_session_secret)
    assert headers["X-Forge-Auth"] == secret and len(secret) >= 32
    environment = [call for call in calls if call[0] == "run"][0][3]
    assert environment["APP_FORWARD_SECRET"] == secret


def test_identity_headers_need_the_shared_secret():
    assert identity_headers("", {"id": "11111111-1111-4111-8111-111111111111",
                                 "email": "a@example.com", "admin": False}) == {}
    with pytest.raises(ValueError):
        identity_headers("s", {"id": "11111111-1111-4111-8111-111111111111",
                               "email": "not an email", "admin": False})


def test_history_counts_preview_deployments_not_generations(preview):
    """開発の区切りは動作確認へ出した時点。生成1回ごとには数えない。"""
    client, sessions, calls, state = preview
    project_id, job_id = generated_job(sessions, client)
    assert client.get(f"/api/projects/{project_id}/deployments").json() == []
    client.post(f"/api/projects/{project_id}/jobs/{job_id}/preview", json={})
    client.post(f"/api/projects/{project_id}/jobs/{job_id}/preview", json={})
    history = client.get(f"/api/projects/{project_id}/deployments").json()
    assert len(history) == 2
    assert history[0]["job_id"] == job_id and history[0]["revision"] == 1
    # 生成しただけの版は履歴に出ない。
    with sessions.begin() as db:
        record = db.get(Project, project_id)
        db.add(GenerationJob(project_id=record.id, owner_id=record.owner_id, revision=2,
                             specification=INPUT, source_type="local_codex", artifact=BUNDLE,
                             status="generated", instruction="日付の絞り込みを足して"))
    assert len(client.get(f"/api/projects/{project_id}/deployments").json()) == 2
    login(client, "bob@example.com")
    assert client.get(f"/api/projects/{project_id}/deployments").status_code == 404


# ---- 画面から指定する環境変数 -----------------------------------------------

def env_entries(client, project_id):
    return client.get(f"/api/projects/{project_id}/preview/env").json()["entries"]


def put_env(client, project_id, entries):
    return client.put(f"/api/projects/{project_id}/preview/env", json={"entries": entries})


def test_saved_environment_reaches_the_running_preview(preview):
    client, sessions, calls, _ = preview
    project_id, job_id = generated_job(sessions, client)
    assert put_env(client, project_id, [
        {"name": "API_BASE", "value": "https://example.test", "secret": False},
        {"name": "API_KEY", "value": "super-secret", "secret": True}]).status_code == 200
    assert client.post(f"/api/projects/{project_id}/jobs/{job_id}/preview", json={}).status_code == 202
    environment = [call for call in calls if call[0] == "run"][-1][3]
    assert environment["API_BASE"] == "https://example.test"
    assert environment["API_KEY"] == "super-secret"
    # プラットフォームの値は残ったまま。
    assert environment["DATABASE_URL"].startswith("sqlite+pysqlite:")


def test_a_secret_is_not_returned_to_the_screen(preview):
    client, sessions, _, _ = preview
    project_id, _ = generated_job(sessions, client)
    put_env(client, project_id, [{"name": "API_KEY", "value": "super-secret", "secret": True}])
    body = client.get(f"/api/projects/{project_id}/preview/env").text
    assert "super-secret" not in body
    assert env_entries(client, project_id) == [
        {"name": "API_KEY", "secret": True, "value": None, "configured": True}]


def test_a_plain_value_is_returned_as_it_is(preview):
    client, sessions, _, _ = preview
    project_id, _ = generated_job(sessions, client)
    put_env(client, project_id, [{"name": "API_BASE", "value": "https://example.test",
                                  "secret": False}])
    assert env_entries(client, project_id)[0]["value"] == "https://example.test"


def test_editing_a_plain_entry_keeps_the_secret(preview):
    client, sessions, calls, _ = preview
    project_id, job_id = generated_job(sessions, client)
    put_env(client, project_id, [{"name": "API_BASE", "value": "https://old", "secret": False},
                                 {"name": "API_KEY", "value": "kept", "secret": True}])
    # 画面は秘密の値を持っていないので、名前と secret だけを送り返す。
    assert put_env(client, project_id, [
        {"name": "API_BASE", "value": "https://new", "secret": False},
        {"name": "API_KEY", "secret": True}]).status_code == 200
    client.post(f"/api/projects/{project_id}/jobs/{job_id}/preview", json={})
    environment = [call for call in calls if call[0] == "run"][-1][3]
    assert environment["API_BASE"] == "https://new" and environment["API_KEY"] == "kept"


def test_platform_keys_are_refused_with_a_readable_reason(preview):
    client, sessions, _, _ = preview
    project_id, _ = generated_job(sessions, client)
    response = put_env(client, project_id, [{"name": "DATABASE_URL", "value": "postgres://x",
                                             "secret": False}])
    assert response.status_code == 422 and "Koyorina" in response.json()["error"]


def test_another_owner_cannot_read_or_write_the_environment(preview):
    client, sessions, _, _ = preview
    project_id, _ = generated_job(sessions, client)
    put_env(client, project_id, [{"name": "API_KEY", "value": "s", "secret": True}])
    login(client, "bob@example.com")
    assert client.get(f"/api/projects/{project_id}/preview/env").status_code == 404
    assert put_env(client, project_id, [{"name": "X", "value": "1", "secret": False}]).status_code == 404


def test_the_screen_is_told_a_restart_is_needed(preview):
    client, sessions, _, _ = preview
    project_id, _ = generated_job(sessions, client)
    body = put_env(client, project_id, [{"name": "A", "value": "1", "secret": False}]).json()
    assert body["restart_required"] is True


def test_a_restart_picks_up_the_latest_values(preview):
    client, sessions, calls, _ = preview
    project_id, job_id = generated_job(sessions, client)
    client.post(f"/api/projects/{project_id}/jobs/{job_id}/preview", json={})
    put_env(client, project_id, [{"name": "API_BASE", "value": "https://after", "secret": False}])
    assert client.post(f"/api/projects/{project_id}/preview/restart", json={}).status_code == 202
    assert [c for c in calls if c[0] == "run"][-1][3]["API_BASE"] == "https://after"


def test_a_command_runs_in_the_preview_and_is_recorded_with_its_text(preview, monkeypatch):
    """打てる口を開けるなら、何を打ったかが残らなければならない。

    「誰かが何かした」だけの記録では、後から追えない。実行したコマンドまで残す
    （そのために audit_events に detail を足した）。
    """
    client, sessions, calls, state = preview
    project_id, job_id = generated_job(sessions, client)
    client.post(f"/api/projects/{project_id}/jobs/{job_id}/preview", json={})

    async def execute(pid, command):
        calls.append(("exec", str(pid), command))
        return {"command": command, "stdout": "ok\n", "stderr": "", "exit_code": 0}

    monkeypatch.setattr("backend.core.preview_runtime.execute", execute)
    result = client.post(f"/api/projects/{project_id}/preview/exec",
                         json={"command": "npm run build"})
    assert result.status_code == 200
    assert result.json()["stdout"] == "ok\n" and result.json()["exit_code"] == 0
    assert ("exec", project_id, "npm run build") in calls
    with sessions() as db:
        event = db.query(Audit).filter(Audit.action == "preview.command").one()
        assert event.resource_id == project_id and event.detail == "npm run build"


def test_a_reader_cannot_run_commands(preview, monkeypatch):
    """プレビューの中は書き換えられる。閲覧だけの人には渡さない。"""
    client, sessions, calls, state = preview
    project_id, _ = generated_job(sessions, client)

    async def execute(pid, command):
        calls.append(("exec", str(pid), command))
        return {"command": command, "stdout": "", "stderr": "", "exit_code": 0}

    monkeypatch.setattr("backend.core.preview_runtime.execute", execute)
    login(client, "bob@example.com")
    assert client.post(f"/api/projects/{project_id}/preview/exec",
                       json={"command": "ls"}).status_code == 404
    assert not [call for call in calls if call[0] == "exec"]


def test_a_command_is_one_line_only(preview):
    """貼り付けた複数行が一度に走ると、打ったつもりのないものまで実行される。"""
    client, sessions, _, _ = preview
    project_id, job_id = generated_job(sessions, client)
    client.post(f"/api/projects/{project_id}/jobs/{job_id}/preview", json={})
    for bad in ("ls\nrm -rf /", "", " " * 5 + "\n"):
        assert client.post(f"/api/projects/{project_id}/preview/exec",
                           json={"command": bad}).status_code == 422


def test_an_administrator_can_stop_a_preview_someone_left_running(preview):
    """オーナーが席を外したまま動かしっぱなし、を片付けられること。

    同時に動かせる数には上限がある。1つ放置されると他の人が使えなくなるので、
    止める手段が誰にも無い、という状態を作らない。
    """
    client, sessions, calls, state = preview
    project_id, job_id = generated_job(sessions, client, owner="bob@example.com")
    client.post(f"/api/projects/{project_id}/jobs/{job_id}/preview", json={})
    assert [call for call in calls if call[0] == "run"]

    login(client, "alice@example.com")   # 管理者。オーナーでも共同開発者でもない
    stopped = client.request("DELETE", f"/api/projects/{project_id}/preview", json={})
    assert stopped.status_code == 200
    assert ("remove", project_id) in calls
    with sessions() as db:
        assert db.query(Audit).filter(Audit.action == "preview.stopped",
                                      Audit.resource_id == project_id).count() == 1


def test_an_administrator_still_cannot_run_or_change_the_app(preview):
    """止められることと、書き換えられることは別。管理者に編集権は渡さない。"""
    client, sessions, calls, state = preview
    project_id, job_id = generated_job(sessions, client, owner="bob@example.com")
    login(client, "alice@example.com")
    calls.clear()
    assert client.post(f"/api/projects/{project_id}/jobs/{job_id}/preview",
                       json={}).status_code == 404
    assert client.post(f"/api/projects/{project_id}/preview/restart", json={}).status_code == 404
    # 作業ディスクの削除も渡さない。止めるのと違って、消したものは戻らない。
    assert client.request("DELETE", f"/api/projects/{project_id}/preview/workspace",
                          json={}).status_code == 404
    assert not [call for call in calls if call[0] == "run"]


def test_the_generated_app_installs_through_the_checked_registry(preview, monkeypatch):
    """生成アプリの依存も、検査済みのレジストリを通すこと。

    生成されたコードが何を import するかは事前に分からない。取得元だけは
    こちらで決めておく。uv は UV_DEFAULT_INDEX、npm は NPM_CONFIG_* を読む。
    """
    client, sessions, calls, state = preview
    settings = client.app.state.settings
    client.app.state.settings = settings.model_copy(update={
        "preview_npm_registry": "https://npm.example",
        "preview_npm_min_release_age": "7",
        "preview_pypi_index": "https://pypi.example/simple/"})
    project_id, job_id = generated_job(sessions, client)
    client.post(f"/api/projects/{project_id}/jobs/{job_id}/preview", json={})
    environment = [call for call in calls if call[0] == "run"][0][3]
    assert environment["NPM_CONFIG_REGISTRY"] == "https://npm.example"
    assert environment["NPM_CONFIG_MIN_RELEASE_AGE"] == "7"
    assert environment["UV_DEFAULT_INDEX"] == "https://pypi.example/simple/"


def test_nothing_is_forced_when_no_registry_is_configured(preview):
    """未設定なら何も渡さない。空文字を渡すと、逆に取得できなくなる。"""
    client, sessions, calls, state = preview
    project_id, job_id = generated_job(sessions, client)
    client.post(f"/api/projects/{project_id}/jobs/{job_id}/preview", json={})
    environment = [call for call in calls if call[0] == "run"][0][3]
    assert not [key for key in environment if key.startswith(("NPM_CONFIG", "UV_"))]


def test_commands_are_refused_unless_enabled(preview, monkeypatch):
    """実行管理のコマンドは既定で閉じている。APIで断り、実行も記録もしない。"""
    client, sessions, calls, state = preview
    project_id, job_id = generated_job(sessions, client)
    client.post(f"/api/projects/{project_id}/jobs/{job_id}/preview", json={})

    async def execute(pid, command):
        calls.append(("exec", str(pid), command))
        return {"command": command, "stdout": "", "stderr": "", "exit_code": 0}

    monkeypatch.setattr("backend.core.preview_runtime.execute", execute)
    client.app.state.settings = client.app.state.settings.model_copy(update={"preview_shell_enabled": False})
    assert client.get("/api/config").json()["preview_shell_enabled"] is False
    response = client.post(f"/api/projects/{project_id}/preview/exec", json={"command": "ls"})
    assert response.status_code == 404
    assert not [call for call in calls if call[0] == "exec"]
    with sessions() as db:
        assert db.scalar(select(Audit).where(Audit.action == "preview.command")) is None
