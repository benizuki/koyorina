import io
import zipfile
from uuid import uuid4
import pytest
from sqlalchemy import select
from backend.core.db import Audit, GenerationJob, Tenant, User, UserTenant
from backend.tests.test_api import context, login, INPUT
from backend.tests.test_codex_generation import BUNDLE


@pytest.fixture
def controller_mock(monkeypatch):
    state = {"status": "connected", "job_status": "generating", "calls": []}
    async def call(settings, user_id, method, path, body=None, **kwargs):
        state["calls"].append((user_id, method, path, body))
        if path == "/account":
            return {"status": state["status"], "generator": state.get("generator", "codex")}
        if "/attachments" in path:
            state["attachments"] = state.get("attachments", [])
            if path.endswith("/remove"):
                state["attachments"] = [a for a in state["attachments"] if a["name"] != body["name"]]
                return {"status": "removed"}
            if method == "POST":
                state["attachments"].append({"name": body["name"], "bytes": len(body["content"])})
                return {"status": "added", "name": body["name"]}
            return {"attachments": state["attachments"]}
        if "/files" in path:
            return {"status": "listed", "files": ["backend/main.py"], "truncated": False}
        if path.endswith("/bundle"):
            return BUNDLE
        if path.endswith("/progress"):
            return {"events": [], "last_response_at": None, "response_bytes": 0, "truncated": False}
        if path.startswith("/jobs"):
            return {"status": state["job_status"], **({"usage": state["usage"]} if "usage" in state else {}),
                    **state.get("report", {})}
        return {"ok": True}
    monkeypatch.setattr("backend.api.generation.controller", call)
    return state


def allow_local_codex(client):
    """手元のCodexでの開発は既定で閉じている。機能そのものを試すテストでだけ開ける。"""
    client.app.state.settings = client.app.state.settings.model_copy(
        update={"local_codex_enabled": True})


def approved(client):
    login(client)
    project = client.post("/api/projects", json=INPUT).json()
    client.post(f"/api/projects/{project['id']}/approve", json={"revision": 1})
    return "/api/projects/" + project["id"]


def test_login_required_no_job_created(context, controller_mock):
    client, sessions = context
    path = approved(client)
    controller_mock["status"] = "disconnected"
    assert client.post(path + "/generate", json={}).status_code == 409
    assert client.get(path + "/jobs").json() == []
    assert all(call[2] == "/account" for call in controller_mock["calls"])


def test_gemini_does_not_need_the_chatgpt_connection(context, controller_mock):
    """Geminiは運営者のGCPで動く。本人のCodex接続とは関係がない。"""
    client, sessions = context
    path = approved(client)
    controller_mock["status"] = "disconnected"
    assert client.post(path + "/generate", json={"provider": "gemini"}).status_code == 202
    # 運用側の既定がGeminiなら、モデルを選ばなくても接続を求めない。
    controller_mock["generator"] = "gemini"
    assert client.post(path + "/generate", json={}).status_code == 409  # 実行中の生成がある
    with sessions.begin() as db:
        for job in db.query(GenerationJob).all():
            job.status = "generated"
    assert client.post(path + "/generate", json={}).status_code == 202


def test_codex_disabled_user_can_only_dispatch_gemini(context, controller_mock):
    client, sessions = context
    from backend.core.db import User
    with sessions.begin() as db:
        bob = db.query(User).filter_by(email="bob@example.com").one()
        tenant_id = db.scalar(select(Tenant.id))
        if tenant_id and not db.get(UserTenant, (bob.id, tenant_id)):
            db.add(UserTenant(user_id=bob.id, tenant_id=tenant_id, role="developer"))
    login(client, "bob@example.com")
    project = client.post("/api/projects", json=INPUT).json()
    path = "/api/projects/" + project["id"]
    client.post(path + "/approve", json={"revision": 1})
    with sessions.begin() as db:
        db.scalar(select(User).where(User.email == "bob@example.com")).codex_enabled = False
    controller_mock["calls"].clear()
    refused = client.post(path + "/generate", json={"provider": "codex"})
    assert refused.status_code == 403
    assert not controller_mock["calls"]
    allowed = client.post(path + "/generate", json={"provider": "gemini"})
    assert allowed.status_code == 202
    assert not any(call[2] == "/account" for call in controller_mock["calls"])
    assert allowed.json()["provider"] == "gemini"


def codex_off(client):
    """CODEX_ENABLED=false。全員のCodexを止める。"""
    client.app.state.settings = client.app.state.settings.model_copy(update={"codex_enabled": False})


def test_codex_disabled_everywhere_dispatches_gemini_without_asking_for_codex(context, controller_mock):
    """全体でCodexを止めたら、モデル未指定の依頼はGeminiへ送り、Codexの接続を確かめに行かない。"""
    client, sessions = context
    path = approved(client)
    codex_off(client)
    controller_mock["calls"].clear()
    assert client.post(path + "/generate", json={"provider": "codex"}).status_code == 403
    started = client.post(path + "/generate", json={})
    assert started.status_code == 202 and started.json()["provider"] == "gemini"
    assert not any(call[2] == "/account" for call in controller_mock["calls"])


def test_runtime_state_is_read_even_when_codex_is_off(context, monkeypatch):
    """生成ワーカーはGemini等でも動く。Codexを止めても、動いている状態を「停止」と出さない。"""
    client, _ = context
    running = {"state": "running", "pods": 1, "running": 1, "starting": 0, "errors": 0}

    async def runtime_call(settings, user_id, method, path, body=None, **kwargs):
        return {name: dict(running) for name in PROVIDERS}

    monkeypatch.setattr("backend.api.codex.controller", runtime_call)
    monkeypatch.setattr("backend.api.codex.gemini_client.available_in", lambda settings: True)
    login(client)
    codex_off(client)
    client.app.state.settings = client.app.state.settings.model_copy(update={
        "codex_controller_url": "http://koyorina-codex-controller.koyorina-codex.svc:8080"})
    result = client.get("/api/codex/runtimes").json()
    assert result["codex"]["state"] == "unavailable"
    assert result["gemini"]["state"] == "running"


def test_generation_snapshot_double_click_and_owner_isolation(context, controller_mock):
    client, sessions = context
    path = approved(client)
    result = client.post(path + "/generate", json={})
    assert result.status_code == 202, result.text
    job = result.json()
    assert job["status"] == "generating"
    assert client.post(path + "/generate", json={}).status_code == 409
    assert len([call for call in controller_mock["calls"] if call[2] == "/jobs/start"]) == 1
    with sessions() as db:
        record = db.get(GenerationJob, job["id"])
        assert record.specification == {**INPUT, "requirements": [], "tables": [{"name": INPUT["name"],
            "kind": "record", "fields": INPUT["fields"]}]}
    login(client, "bob@example.com")
    assert client.get(path + "/jobs").status_code == 404
    assert client.post(path + f"/jobs/{job['id']}/refresh", json={}).status_code == 404
    assert client.get(path + f"/jobs/{job['id']}/source").status_code == 404
    calls_before = len(controller_mock["calls"])
    assert client.get(path + f"/jobs/{job['id']}/progress").status_code == 404
    assert len(controller_mock["calls"]) == calls_before


def test_starting_job_remains_visible_while_worker_pod_is_preparing(context, controller_mock, monkeypatch):
    client, _ = context
    path = approved(client)
    controller_mock["job_status"] = "starting"
    job = client.post(path + "/generate", json={"provider": "gemini"}).json()

    async def preparing(*args, **kwargs):
        from fastapi import HTTPException
        raise HTTPException(503, "worker pod is not ready")

    monkeypatch.setattr("backend.api.generation.controller", preparing)
    result = client.post(path + f"/jobs/{job['id']}/refresh", json={})
    assert result.status_code == 200
    assert result.json()["status"] == "starting"


def test_progress_waits_for_async_worker_dispatch(context, controller_mock, monkeypatch):
    client, _ = context
    path = approved(client)
    job = client.post(path + "/generate", json={"provider": "gemini"}).json()
    from backend.api.generation import controller as original

    async def not_yet_dispatched(settings, user_id, method, target, *args, **kwargs):
        if target.endswith("/progress"):
            from fastapi import HTTPException
            raise HTTPException(404, "生成履歴が見つかりません。")
        return await original(settings, user_id, method, target, *args, **kwargs)

    monkeypatch.setattr("backend.api.generation.controller", not_yet_dispatched)
    pending = client.get(path + f"/jobs/{job['id']}/progress")
    assert pending.status_code == 200
    assert pending.json()["events"] == []

    monkeypatch.setattr("backend.api.generation.controller", original)
    ready = client.get(path + f"/jobs/{job['id']}/progress")
    assert ready.status_code == 200
    assert any(call[2] == f"/jobs/{job['id']}/progress" for call in controller_mock["calls"])


def test_generated_is_not_deployed_and_source_download(context, controller_mock):
    client, _ = context
    path = approved(client)
    job = client.post(path + "/generate", json={}).json()
    assert client.get(path + f"/jobs/{job['id']}/source").status_code == 409
    controller_mock["job_status"] = "generated"
    controller_mock["usage"] = {"input_tokens": 80, "output_tokens": 20,
                                "cached_tokens": 10, "total_tokens": 100}
    controller_mock["report"] = {"summary": "一覧を作りました。", "next_steps": ["集計"]}
    updated = client.post(path + f"/jobs/{job['id']}/refresh", json={}).json()
    assert updated["status"] == "generated"
    assert updated["usage"]["total_tokens"] == 100
    # 最後の報告と見送った機能は、チャットの返事として残る。
    assert updated["summary"] == "一覧を作りました。" and updated["next_steps"] == ["集計"]
    source = client.get(path + f"/jobs/{job['id']}/source")
    assert source.status_code == 200 and source.headers["content-type"] == "application/zip"
    assert source.content[:2] == b"PK"
    assert client.get(path + f"/jobs/{job['id']}/progress").json()["events"] == []


def bundle_zip(root="local-app"):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for item in BUNDLE["files"]:
            archive.writestr(f"{root}/{item['path']}", item["content"])
        archive.writestr(f"{root}/.env", "SECRET=must-not-be-stored")
    return buffer.getvalue()


def test_local_codex_package_upload_and_download(context, controller_mock):
    client, sessions = context
    allow_local_codex(client)
    path = approved(client)
    package = client.get(path + "/local-package")
    assert package.status_code == 200 and package.content[:2] == b"PK"
    with zipfile.ZipFile(io.BytesIO(package.content)) as archive:
        # まだ生成していないので、コードは無い。実行環境にも聞きに行かない。
        assert {"KOYORINA-README.md", "AGENTS.md", "APP-FORGE-SPEC.json", ".gitignore"} == set(archive.namelist())
        assert "alice@example.com" not in "".join(archive.read(name).decode() for name in archive.namelist())
    assert not any(call[2].endswith("/archive") for call in controller_mock["calls"])
    uploaded = client.post(path + "/local-artifact", content=bundle_zip(),
                           headers={"Content-Type": "application/zip"})
    assert uploaded.status_code == 201, uploaded.text
    job = uploaded.json()
    assert job["status"] == "generated" and job["source_type"] == "local_codex"
    source = client.get(path + f"/jobs/{job['id']}/source")
    assert source.status_code == 200 and b"must-not-be-stored" not in source.content
    assert not any(call[2].endswith("/bundle") for call in controller_mock["calls"])
    with sessions() as db:
        stored = db.get(GenerationJob, job["id"])
        assert stored.artifact and stored.source_type == "local_codex"
    # 戻したあとは、そのコードも作業パッケージに入る。手順書と規約はアプリのコードへ混ざらない。
    again = client.get(path + "/local-package")
    with zipfile.ZipFile(io.BytesIO(again.content)) as archive:
        names = set(archive.namelist())
    assert {item["path"] for item in BUNDLE["files"]} <= names
    assert {"KOYORINA-README.md", "AGENTS.md", "APP-FORGE-SPEC.json"} <= names
    reupload = client.post(path + "/local-artifact", content=again.content,
                           headers={"Content-Type": "application/zip"})
    assert reupload.status_code == 201, reupload.text
    reuploaded = client.get(path + f"/jobs/{reupload.json()['id']}/source")
    with zipfile.ZipFile(io.BytesIO(reuploaded.content)) as archive:
        assert not {"KOYORINA-README.md", "AGENTS.md", "APP-FORGE-SPEC.json"} & set(archive.namelist())


def test_local_upload_requires_zip_and_current_approval(context, controller_mock):
    client, _ = context
    allow_local_codex(client)
    path = approved(client)
    assert client.post(path + "/local-artifact", content=b"not-json").status_code == 415
    assert client.post(path + "/local-artifact", content=b"not-a-zip",
                       headers={"Content-Type": "application/zip"}).status_code == 422


def test_codex_auth_routes_require_invitation_and_csrf(context):
    client, _ = context
    assert client.get("/api/codex/status").status_code == 401
    assert client.post("/api/codex/login", json={}).status_code == 401
    assert client.post("/api/codex/logout", json={}).status_code == 401
    login(client)
    assert client.get("/api/codex/status").json()["status"] == "unavailable"
    assert client.post("/api/codex/login", json={}, headers={"Origin": "https://evil.test"}).status_code == 403


PROVIDERS = ("codex", "gemini", "antigravity", "openai_compatible", "claude")


def test_runtime_bar_requires_login_and_returns_every_provider(context, monkeypatch):
    client, _ = context
    assert client.get("/api/codex/runtimes").status_code == 401

    async def runtime_call(settings, user_id, method, path, body=None, **kwargs):
        assert method == "GET" and path == "/runtimes"
        return {name: {"state": "stopped", "pods": 0, "running": 0,
                       "starting": 0, "errors": 0} for name in PROVIDERS}

    monkeypatch.setattr("backend.api.codex.controller", runtime_call)
    login(client)
    result = client.get("/api/codex/runtimes")
    assert result.status_code == 200
    # 生成AIの選択肢はすべて返す。使えないものは消さずに unavailable で示す。
    assert set(result.json()) == set(PROVIDERS)


def test_login_warmup_starts_only_a_member_tenant_runtime(context, monkeypatch):
    client, sessions = context
    calls = []

    async def runtime_call(settings, user_id, method, path, body=None, **kwargs):
        calls.append((str(user_id), method, path, str(kwargs.get("tenant_id"))))
        return {"status": "starting"}

    monkeypatch.setattr("backend.api.codex.controller", runtime_call)
    login(client)
    tenant_id = str(uuid4())
    with sessions.begin() as db:
        user_id = db.scalar(select(User.id).where(User.email == "alice@example.com"))
        db.add(Tenant(id=tenant_id, name="Warmup tenant"))
        db.add(UserTenant(user_id=user_id, tenant_id=tenant_id))
    result = client.post(f"/api/codex/runtimes/{tenant_id}/start", json={})
    assert result.status_code == 202 and result.json() == {"status": "starting"}
    assert calls[0][1:] == ("POST", "/runtime/start", tenant_id)
    assert client.post(f"/api/codex/runtimes/{uuid4()}/start", json={}).status_code == 403


def test_polling_endpoints_do_not_hold_a_database_session(context, controller_mock):
    """状態確認の通信中にDB接続を握らない。握るとプールが尽きてアプリ全体が止まる。"""
    client, sessions = context
    path = approved(client)
    job = client.post(path + "/generate", json={}).json()
    holding = {}

    async def call(settings, user_id, method, target, body=None, **kwargs):
        # 通信中に別のセッションが使えることを確かめる。
        with sessions() as db:
            holding[target] = db.scalar(select(GenerationJob.id).where(GenerationJob.id == job["id"]))
        if target.endswith("/progress"):
            return {"events": [], "last_response_at": None, "response_bytes": 0, "truncated": False}
        return {"status": "generating"}

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr("backend.api.generation.controller", call)
    try:
        assert client.post(path + f"/jobs/{job['id']}/refresh", json={}).status_code == 200
        assert client.get(path + f"/jobs/{job['id']}/progress").status_code == 200
    finally:
        monkeypatch.undo()
    assert holding[f"/jobs/{job['id']}"] == job["id"]
    assert holding[f"/jobs/{job['id']}/progress"] == job["id"]


def test_instruction_requires_generated_code_and_is_recorded(context, controller_mock):
    """変更依頼は生成済みのコードに対してだけ送れる。指示は履歴に残す。"""
    client, sessions = context
    path = approved(client)
    assert client.post(path + "/messages", json={"text": "一覧に日付の絞り込みを足して"}).status_code == 409
    controller_mock["job_status"] = "generated"
    first = client.post(path + "/generate", json={}).json()
    with sessions.begin() as db:
        db.get(GenerationJob, first["id"]).status = "generated"
    result = client.post(path + "/messages", json={"text": "一覧に日付の絞り込みを足して"})
    assert result.status_code == 202, result.text
    assert result.json()["instruction"] == "一覧に日付の絞り込みを足して"
    dispatched = [call for call in controller_mock["calls"] if call[2] == "/jobs/start"][-1][3]
    # エージェントは同じプロジェクトの作業場所で続きを行う。
    assert dispatched["project_id"] == first["project_id"]
    assert dispatched["instruction"] == "一覧に日付の絞り込みを足して"
    assert client.post(path + "/messages", json={"text": ""}).status_code == 422
    login(client, "bob@example.com")
    assert client.post(path + "/messages", json={"text": "他人のアプリを変更"}).status_code == 404


def test_instruction_accepts_code_generated_by_a_managed_non_codex_provider(context, controller_mock):
    client, sessions = context
    path = approved(client)
    controller_mock["job_status"] = "generated"
    first = client.post(path + "/generate", json={"provider": "gemini",
                                                    "model": "gemini-3.8-flash"}).json()
    with sessions.begin() as db:
        job = db.get(GenerationJob, first["id"])
        job.status = "generated"
        job.source_type = "managed_gemini"

    result = client.post(path + "/messages", json={"text": "プレビューのエラーを直して"})
    assert result.status_code == 202, result.text


def test_instruction_reuses_previous_provider_when_provider_is_omitted(context, controller_mock):
    client, sessions = context
    path = approved(client)
    controller_mock["job_status"] = "generated"
    first = client.post(path + "/generate", json={"provider": "gemini",
                                                    "model": "gemini-3.8-flash"}).json()
    with sessions.begin() as db:
        job = db.get(GenerationJob, first["id"])
        job.status = "generated"
        job.provider = "gemini"

    result = client.post(path + "/messages", json={"text": "Geminiでプレビューのエラーを直して"})
    assert result.status_code == 202, result.text
    dispatched = [call for call in controller_mock["calls"] if call[2] == "/jobs/start"][-1][3]
    assert dispatched["generator"] == "gemini"


def test_improvements_can_be_grouped_into_separate_development_chats(context, controller_mock):
    client, sessions = context
    path = approved(client)
    first_chat, improvement_chat = uuid4(), uuid4()
    controller_mock["job_status"] = "generated"
    first = client.post(path + "/generate", json={"chat_id": str(first_chat)}).json()
    assert first["chat_id"] == str(first_chat)
    with sessions.begin() as db:
        db.get(GenerationJob, first["id"]).status = "generated"
    improvement = client.post(path + "/messages", json={
        "text": "検索条件を追加", "chat_id": str(improvement_chat),
    }).json()
    assert improvement["chat_id"] == str(improvement_chat)
    jobs = client.get(path + "/jobs").json()
    assert {job["chat_id"] for job in jobs} == {str(first_chat), str(improvement_chat)}


def test_request_can_choose_the_provider_and_model(context, controller_mock):
    """依頼ごとに経路とモデルを選べる。値は検証してからエージェントへ渡す。"""
    client, sessions = context
    path = approved(client)
    result = client.post(path + "/generate", json={"provider": "gemini", "model": "gemini-3.8-flash"})
    assert result.status_code == 202, result.text
    sent = [call for call in controller_mock["calls"] if call[2] == "/jobs/start"][-1][3]
    assert sent["generator"] == "gemini" and sent["model"] == "gemini-3.8-flash"
    # 用意していない経路は受け付けない。モデル名の形式は model_settings が弾く。
    assert client.post(path + "/generate", json={"provider": "openai"}).status_code == 422


def test_gemini_35_flash_is_available_and_dispatched(context, controller_mock):
    """3.5 Flashを一覧から選び、そのモデル名をGemini環境へ渡せる。"""
    client, _ = context
    path = approved(client)
    client.app.state.settings.vertex_location = "global"
    models = client.get("/api/codex/models").json()["models"]
    gemini = next(model for model in models if model["id"] == "gemini-3.5-flash")
    assert gemini["label"] == "gemini-3.5-flash（Vertex AI）"
    assert gemini["provider"] == "gemini"
    assert gemini["efforts"] == ["minimal", "low", "medium", "high"]
    assert gemini["default_effort"] == "medium"

    result = client.post(path + "/generate", json={
        "provider": "gemini", "model": "gemini-3.5-flash", "effort": "minimal",
    })
    assert result.status_code == 202, result.text
    sent = [call for call in controller_mock["calls"] if call[2] == "/jobs/start"][-1][3]
    assert sent["generator"] == "gemini" and sent["model"] == "gemini-3.5-flash"
    assert sent["effort"] == "minimal"


def test_default_generator_setting_moves_is_default_to_that_provider(context, controller_mock):
    """DEFAULT_GENERATORで、選択肢の初期選択をCodex以外へ移せる。"""
    client, _ = context
    approved(client)
    client.app.state.settings.vertex_location = "global"
    client.app.state.settings.default_generator = "gemini"
    models = client.get("/api/codex/models").json()["models"]
    defaults = [model for model in models if model["is_default"]]
    assert len(defaults) == 1
    assert defaults[0]["provider"] == "gemini"


def test_default_generator_falls_back_when_the_provider_is_unavailable(context, controller_mock):
    """設定した既定providerが使えない(未接続・未設定)なら、並び順どおりに戻る。"""
    client, _ = context
    approved(client)
    client.app.state.settings.antigravity_enabled = False
    client.app.state.settings.default_generator = "antigravity"
    models = client.get("/api/codex/models").json()["models"]
    assert not any(model["provider"] == "antigravity" for model in models)


def test_antigravity_model_choice_has_a_unique_id_and_dispatches_remote_provider(context, controller_mock):
    client, _ = context
    path = approved(client)
    client.app.state.settings.antigravity_enabled = True
    client.app.state.settings.antigravity_model = "gemini-3.8-flash"
    models = client.get("/api/codex/models").json()["models"]
    option = next(model for model in models if model["provider"] == "antigravity")
    assert option["id"] == "antigravity-gemini-3.8-flash"
    assert option["label"] == "gemini-3.8-flash（Antigravity）"


def test_running_generation_can_be_stopped(context, controller_mock, monkeypatch):
    """実行中の生成を止められる。止めた状態を残し、自動では作り直さない。"""
    client, sessions = context
    path = approved(client)
    job = client.post(path + "/generate", json={}).json()
    assert job["status"] == "generating"

    async def call(settings, user_id, target, *args, **kwargs):
        raise AssertionError("unused")

    async def controller_call(settings, user_id, method, target, body=None, **kwargs):
        controller_mock["calls"].append((user_id, method, target, body))
        if target.endswith("/cancel"):
            return {"status": "failed", "failure_code": "cancelled"}
        return {"status": "generating"}

    monkeypatch.setattr("backend.api.generation.controller", controller_call)
    result = client.post(path + f"/jobs/{job['id']}/cancel", json={})
    assert result.status_code == 200, result.text
    assert result.json()["status"] == "failed"
    assert "中止" in result.json()["error"]
    with sessions() as db:
        assert db.query(Audit).filter(Audit.action == "generation.cancelled",
                                      Audit.resource_id == job["id"]).count() == 1
    # 終わった生成は止める対象にしない。
    assert client.post(path + f"/jobs/{job['id']}/cancel", json={}).json()["status"] == "failed"
    login(client, "bob@example.com")
    assert client.post(path + f"/jobs/{job['id']}/cancel", json={}).status_code == 404


def test_generated_files_can_be_read_in_the_browser(context, controller_mock):
    """生成されたファイルを画面で確かめられる。手元から戻したZIPも同じ形で読む。"""
    client, sessions = context
    path = approved(client)
    listing = client.get(path + "/files")
    assert listing.status_code == 200 and listing.json()["files"] == ["backend/main.py"]
    sent = [call for call in controller_mock["calls"] if "/files" in call[2]][-1]
    assert sent[2].endswith("/files")
    # パスは渡す前に検証する。作業場所の外は指させない。
    assert client.get(path + "/files", params={"path": "../secrets"}).status_code == 422

    # 手元のCodexから戻したZIPは、作業場所ではなくジョブから読む。
    job = client.post(path + "/generate", json={}).json()
    with sessions.begin() as db:
        record = db.get(GenerationJob, job["id"])
        record.status, record.source_type, record.artifact = "generated", "local_codex", BUNDLE
    assert sorted(client.get(path + "/files").json()["files"]) == sorted(f["path"] for f in BUNDLE["files"])
    content = client.get(path + "/files", params={"path": BUNDLE["files"][0]["path"]}).json()
    assert content["status"] == "read" and content["text"] == BUNDLE["files"][0]["content"]


def test_attachments_are_checked_before_they_leave_the_app(context, controller_mock):
    """添付は中身で種類を判断する。扱えないものは実行環境へ渡さない。"""
    import base64
    client, sessions = context
    path = approved(client)
    png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
    binary = {"Content-Type": "application/octet-stream"}
    result = client.post(path + "/attachments", content=png,
                         headers={**binary, "x-file-name": "%E7%94%BB%E9%9D%A2%E6%A1%88.png"})
    assert result.status_code == 200, result.text
    assert client.get(path + "/attachments").json()["attachments"][0]["name"] == "画面案.png"

    # 実行ファイルは渡さない。名前で偽装されていても中身で弾く。
    rejected = client.post(path + "/attachments", content=b"MZ\x90\x00",
                           headers={**binary, "x-file-name": "harmless.png"})
    assert rejected.status_code == 422
    assert client.post(path + "/attachments", content=png, headers=binary).status_code == 422  # 名前なし

    assert client.post(path + "/attachments/remove", json={"name": "画面案.png"}).status_code == 200
    assert client.get(path + "/attachments").json()["attachments"] == []

    # 他人のプロジェクトの添付は見えない。
    login(client, "bob@example.com")
    assert client.get(path + "/attachments").status_code == 404


def test_request_records_the_attachments_it_was_sent_with(context, controller_mock):
    """添付はその依頼に結び付けて残す。履歴でどの依頼に何を添えたか分かるようにする。"""
    import base64
    client, sessions = context
    path = approved(client)
    png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
    client.post(path + "/attachments", content=png,
                headers={"Content-Type": "application/octet-stream",
                         "x-file-name": "%E7%94%BB%E9%9D%A2%E6%A1%88.png"})
    job = client.post(path + "/generate", json={}).json()
    assert job["attachments"] == ["画面案.png"]
    assert client.get(path + "/jobs").json()[0]["attachments"] == ["画面案.png"]


def age_job(sessions, job_id, seconds=600):
    from datetime import datetime, timedelta, timezone
    with sessions.begin() as db:
        db.get(GenerationJob, job_id).created_at = (
            datetime.now(timezone.utc) - timedelta(seconds=seconds))


def test_an_unreachable_worker_never_marks_a_running_generation_as_failed(
        context, controller_mock, monkeypatch):
    """届かないことは、失敗したことではない。

    配備でPodを入れ替えている最中は、実行環境へ問い合わせられない。これを失敗として
    書き込むと、裏でまだ動いている生成が画面から消え、利用者は二重に依頼してしまう。
    実際にそれが起きた（管理側とコントローラを同時に入れ替えたとき）。
    """
    client, sessions = context
    path = approved(client)
    job = client.post(path + "/generate", json={"provider": "gemini"}).json()
    age_job(sessions, job["id"])  # 起動待ちの猶予を過ぎさせる

    async def unreachable(*args, **kwargs):
        from fastapi import HTTPException
        raise HTTPException(503, "worker pod is being replaced")

    monkeypatch.setattr("backend.api.generation.controller", unreachable)
    result = client.post(path + f"/jobs/{job['id']}/refresh", json={}).json()
    assert result["status"] == "generating"
    # 確かめられなかったことは画面へ伝える。黙って「進行中」と出すと、
    # 止まっているのか続いているのか分からない。
    assert result["unconfirmed"] is True
    # 理由もそのまま渡す。固定文に潰すと、待てば済むのかが判断できない。
    assert result["unconfirmed_reason"] == "worker pod is being replaced"
    with sessions() as db:
        assert db.get(GenerationJob, job["id"]).status == "generating"
    # 二重依頼も止まったまま。失敗にしていたら、ここが通ってしまう。
    assert client.post(path + "/generate", json={"provider": "gemini"}).status_code == 409


def test_a_job_the_worker_never_received_says_so_plainly(context, controller_mock, monkeypatch):
    """404は実行環境の確かな答え。ただし理由は「枠や接続」ではない。"""
    client, sessions = context
    path = approved(client)
    job = client.post(path + "/generate", json={"provider": "gemini"}).json()
    age_job(sessions, job["id"])

    async def missing(*args, **kwargs):
        from fastapi import HTTPException
        raise HTTPException(404, "生成履歴が見つかりません。")

    monkeypatch.setattr("backend.api.generation.controller", missing)
    result = client.post(path + f"/jobs/{job['id']}/refresh", json={}).json()
    assert result["status"] == "failed"
    assert "別の生成" in result["error"] and "利用枠" not in result["error"]


def test_a_generation_wrongly_recorded_as_failed_is_put_back(context, controller_mock):
    """一度failedに書き込むと二度と確かめ直せない、では取り返しがつかない。"""
    client, sessions = context
    path = approved(client)
    job = client.post(path + "/generate", json={"provider": "gemini"}).json()
    with sessions.begin() as db:
        db.get(GenerationJob, job["id"]).status = "failed"
    # 実行環境はまだ生成中だと言っている。その答えを採る。
    controller_mock["job_status"] = "generating"
    result = client.post(path + f"/jobs/{job['id']}/refresh", json={}).json()
    assert result["status"] == "generating"
    with sessions() as db:
        assert db.get(GenerationJob, job["id"]).status == "generating"


def test_a_finished_generation_is_never_re_examined(context, controller_mock):
    """取り出し済みの結果を問い合わせ直すと、実行環境の都合で消えうる。"""
    client, sessions = context
    path = approved(client)
    job = client.post(path + "/generate", json={"provider": "gemini"}).json()
    controller_mock["job_status"] = "generated"
    assert client.post(path + f"/jobs/{job['id']}/refresh", json={}).json()["status"] == "generated"
    controller_mock["job_status"] = "failed"
    controller_mock["calls"].clear()
    assert client.post(path + f"/jobs/{job['id']}/refresh", json={}).json()["status"] == "generated"
    assert not controller_mock["calls"]


def test_a_job_stopped_by_the_check_can_be_completed_without_regenerating(context, controller_mock):
    client, sessions = context
    path = approved(client)
    job = client.post(path + "/generate", json={"provider": "gemini"}).json()
    with sessions.begin() as db:
        db.get(GenerationJob, job["id"]).status = "failed"
    controller_mock["job_status"] = "generated"
    controller_mock["calls"].clear()
    result = client.post(path + f"/jobs/{job['id']}/revalidate", json={})
    assert result.status_code == 200, result.text
    assert result.json()["status"] == "generated" and result.json()["error"] is None
    # 推論は依頼しない。検査だけを頼む。
    [(_, method, sent_path, body)] = controller_mock["calls"]
    assert (method, sent_path) == ("POST", f"/jobs/{job['id']}/revalidate")
    assert body["specification"]["name"] == INPUT["name"] and body["requested_by"]
    with sessions() as db:
        assert db.scalar(select(Audit).where(Audit.action == "generation.revalidated",
                                             Audit.resource_id == job["id"])) is not None


def test_only_the_latest_failed_job_can_be_revalidated(context, controller_mock):
    client, sessions = context
    path = approved(client)
    first = client.post(path + "/generate", json={"provider": "gemini"}).json()
    # 進行中・完了済みのジョブは対象外。
    assert client.post(path + f"/jobs/{first['id']}/revalidate", json={}).status_code == 409
    with sessions.begin() as db:
        db.get(GenerationJob, first["id"]).status = "failed"
    second = client.post(path + "/generate", json={"provider": "gemini"}).json()
    with sessions.begin() as db:
        db.get(GenerationJob, second["id"]).status = "failed"
    controller_mock["calls"].clear()
    # 作業場所は後の生成の中身になっている。古いジョブを完了にはしない。
    response = client.post(path + f"/jobs/{first['id']}/revalidate", json={})
    assert response.status_code == 409 and "最新" in response.json()["error"]
    assert not controller_mock["calls"]


def test_revalidation_returns_what_is_still_missing(context, controller_mock, monkeypatch):
    client, sessions = context
    path = approved(client)
    job = client.post(path + "/generate", json={"provider": "gemini"}).json()
    with sessions.begin() as db:
        db.get(GenerationJob, job["id"]).status = "failed"

    async def rejected(settings, user_id, method, sent_path, body=None, **kwargs):
        return {"status": "failed", "failure_code": "validation_entrypoints",
                "problems": ["frontend/src/app.vue: 必須ファイルがありません。"]}

    monkeypatch.setattr("backend.api.generation.controller", rejected)
    result = client.post(path + f"/jobs/{job['id']}/revalidate", json={}).json()
    assert result["status"] == "failed"
    assert result["problems"] == ["frontend/src/app.vue: 必須ファイルがありません。"]
    assert "validation_entrypoints" in result["error"]


def test_local_codex_is_closed_unless_enabled(context, controller_mock):
    """公開サービスでは閉じておく。画面から隠すだけでなく、APIで断る。"""
    import io, zipfile
    client, sessions = context
    path = approved(client)
    assert client.get("/api/config").json()["local_codex_enabled"] is False
    assert client.get(path + "/local-package").status_code == 404
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for item in BUNDLE["files"]:
            archive.writestr(item["path"], item["content"])
    response = client.post(path + "/local-artifact", content=buffer.getvalue(),
                           headers={"Content-Type": "application/zip"})
    assert response.status_code == 404
    assert client.get(path + "/jobs").json() == []


def test_a_zip_registered_before_closing_is_not_previewed(context, controller_mock):
    """無効にする前に登録されたZIPも、プレビューには載せない。"""
    import asyncio
    from types import SimpleNamespace
    from fastapi import HTTPException
    from backend.api.generation import job_bundle
    job = SimpleNamespace(status="generated", source_type="local_codex", artifact=BUNDLE, id=str(uuid4()))
    with pytest.raises(HTTPException) as error:
        asyncio.run(job_bundle(SimpleNamespace(local_codex_enabled=False), None, job))
    assert error.value.status_code == 409 and "手元のCodex" in error.value.detail


def test_model_list_tells_the_screen_when_codex_is_still_starting(context, monkeypatch):
    """起動中はCodexの選択肢が無い理由を伝える。画面はそれを見て取り直す。"""
    client, _ = context
    login(client)
    client.app.state.settings = client.app.state.settings.model_copy(update={
        "codex_enabled": True, "codex_controller_url": "http://127.0.0.1:8091"})
    replies = {"value": {"models": [], "status": "preparing"}}

    async def call(settings, user_id, method, path, body=None, **kwargs):
        if isinstance(replies["value"], Exception):
            raise replies["value"]
        return replies["value"]
    monkeypatch.setattr("backend.api.codex.controller", call)
    assert client.get("/api/codex/models").json()["codex_status"] == "preparing"
    replies["value"] = {"models": [{"id": "gpt-6.1-sol", "label": "GPT-6.1-Sol", "provider": "codex",
                                    "description": "", "efforts": ["low"], "default_effort": "low",
                                    "is_default": True}]}
    body = client.get("/api/codex/models").json()
    assert body["codex_status"] == "ready" and body["models"][0]["id"] == "gpt-6.1-sol"
    from fastapi import HTTPException
    replies["value"] = HTTPException(503, "down")
    assert client.get("/api/codex/models").json()["codex_status"] == "unavailable"
