"""Provider isolation, shared history and cross-worker routing without inference."""
import asyncio
import inspect
import base64
import json
from threading import Event
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend.worker.agent import Agent, AgentSettings, GenerationInput, create_agent
from backend.worker.controller import (ControllerSettings, LEGACY_ROUTES, Provisioner,
                                       create_controller, resources, worker_route)
from backend.domain.interview import GeminiInterviewDecision
from backend.domain.projects import ProjectInput
from backend.domain.tenant_storage import generation_claim


TOKEN = "test-only-" + "x" * 40
IMAGE = "registry.example.com/koyorina-agent@sha256:" + "a" * 64


def config(**values):
    return ControllerSettings(_env_file=None, token=TOKEN, agent_image=IMAGE,
                              vertex_project="test-project", generator="gemini", **values)


def test_one_worker_supports_both_providers_and_shared_history():
    """Codex and Gemini use one user+tenant worker and the same durable workspace."""
    user, tenant = uuid4(), uuid4()
    codex = resources(user, config(), "codex", tenant=str(tenant))
    gemini = resources(user, config(), "gemini", tenant=str(tenant))
    assert codex["pods"]["metadata"]["name"] == gemini["pods"]["metadata"]["name"]
    assert codex["pods"]["metadata"]["labels"]["forge-route"] == "shared"
    assert codex["persistentvolumeclaims"]["metadata"]["name"] == gemini["persistentvolumeclaims"]["metadata"]["name"]
    mounts = gemini["pods"]["spec"]["containers"][0]["volumeMounts"]
    # The shared worker receives the tenant workspace and the user's Codex credential PVC.
    assert {m["subPath"] for m in mounts if m["name"] == "workspaces"} == {"projects", "jobs", "history"}
    assert any(m["name"] == "data" for m in mounts)
    assert any(v["name"] == "data" for v in gemini["pods"]["spec"]["volumes"])
    # 履歴が使い捨ての領域に載っていないこと。ここが今回の抜けだった。
    for pod in (codex, gemini):
        history = next(m for m in pod["pods"]["spec"]["containers"][0]["volumeMounts"]
                       if m["mountPath"] == "/data/history")
        assert history["name"] == "workspaces"
        volume = next(v for v in pod["pods"]["spec"]["volumes"] if v["name"] == "workspaces")
        assert "emptyDir" not in volume and "persistentVolumeClaim" in volume
    # Codex側は認証も作業場所も要る。両方を別々のPVCから受け取る。
    codex_mounts = codex["pods"]["spec"]["containers"][0]["volumeMounts"]
    assert {m["subPath"] for m in codex_mounts if m["name"] == "workspaces"} == {"projects", "jobs", "history"}
    assert any(m["name"] == "data" and m["mountPath"] == "/data" for m in codex_mounts)
    for pod in (codex, gemini):
        initializer = pod["pods"]["spec"]["initContainers"][0]
        assert initializer["name"] == "prepare-storage"
        assert initializer["volumeMounts"] == [{"name": "workspaces", "mountPath": "/data"}]
    assert gemini["pods"]["spec"]["securityContext"]["seccompProfile"]["type"] == "Localhost"
    assert codex["pods"]["spec"]["securityContext"]["seccompProfile"]["type"] == "Localhost"
    assert any(v["name"] == "vertex" for v in codex["pods"]["spec"]["volumes"])
    assert any(v["name"] == "vertex" for v in gemini["pods"]["spec"]["volumes"])


def test_antigravity_never_uses_a_pre_shared_legacy_worker_name():
    """The remote provider only has the shared tenant worker; cleanup must not
    ask the legacy naming helper to construct an unsupported provider name."""
    assert LEGACY_ROUTES == ("codex", "gemini")
    assert "antigravity" not in LEGACY_ROUTES


def test_tenants_get_different_claims_and_a_pod_mounts_only_its_tenant():
    user, first, second = uuid4(), uuid4(), uuid4()
    one = resources(user, config(), "codex", tenant=str(first))
    two = resources(user, config(), "codex", tenant=str(second))
    assert one["pods"]["metadata"]["name"] != two["pods"]["metadata"]["name"]
    assert one["workspaces"]["metadata"]["name"] == generation_claim(first)
    assert two["workspaces"]["metadata"]["name"] == generation_claim(second)
    mounted = {volume.get("persistentVolumeClaim", {}).get("claimName")
               for volume in one["pods"]["spec"]["volumes"]}
    assert generation_claim(first) in mounted
    assert generation_claim(second) not in mounted
    assert "services" not in one and "services" not in two


def test_authentication_worker_has_no_tenant_claim_or_mount():
    shape = resources(uuid4(), config(), "codex", auth_only=True)
    assert "workspaces" not in shape
    assert "forge-tenant" not in shape["pods"]["metadata"]["labels"]
    mounts = shape["pods"]["spec"]["containers"][0]["volumeMounts"]
    assert not any(mount["name"] == "workspaces" for mount in mounts)
    assert "initContainers" not in shape["pods"]["spec"]


def test_runtime_status_distinguishes_generation_pods_from_authentication():
    user, tenant = uuid4(), uuid4()
    provisioner = Provisioner(config())
    pods = [
        {"metadata": {"labels": {"forge-user": str(user), "forge-route": "codex",
                                  "forge-purpose": "authentication"}},
         "status": {"phase": "Running", "conditions": [{"type": "Ready", "status": "True"}]}},
        {"metadata": {"labels": {"forge-user": str(user), "forge-route": "shared",
                                  "forge-tenant": str(tenant)}},
         "status": {"phase": "Running", "conditions": [{"type": "Ready", "status": "True"}]}},
    ]

    async def kube(method, resource, name="", body=None, query="", **kwargs):
        assert method == "GET" and resource == "pods" and f"forge-user={user}" in query
        return {"items": pods}

    provisioner.kube = kube
    result = asyncio.run(provisioner.runtime_status(user))
    assert result["codex"] == {"state": "running", "pods": 1, "running": 1,
                               "starting": 0, "errors": 0}
    assert result["gemini"] == {"state": "running", "pods": 1, "running": 1,
                                "starting": 0, "errors": 0}


def test_runtime_endpoint_reports_accepted_job_before_its_pod_exists():
    app = create_controller(config())
    user, tenant, job = uuid4(), uuid4(), uuid4()

    async def stopped(_user):
        return {route: {"state": "stopped", "pods": 0, "running": 0,
                        "starting": 0, "errors": 0} for route in ("codex", "gemini")}

    app.state.provisioner.runtime_status = stopped
    app.state.pending_jobs[(tenant, user, job)] = (None, "gemini")
    with TestClient(app, headers={"Authorization": "Bearer " + TOKEN}) as client:
        result = client.get(f"/users/{user}/runtimes").json()
    assert result["codex"]["state"] == "stopped"
    assert result["gemini"]["state"] == "starting"


def test_generation_migration_mounts_only_source_and_target_tenants():
    source, target, project, migration = uuid4(), uuid4(), uuid4(), uuid4()
    provisioner = Provisioner(config())
    created = []

    async def kube(method, resource, name="", body=None, **kwargs):
        if method == "GET" and resource == "persistentvolumeclaims":
            return None
        if method == "POST":
            created.append(body)
            return body
        if method == "GET" and resource == "pods":
            return {"status": {"phase": "Succeeded"}}
        return {}

    provisioner.kube = kube
    result = asyncio.run(provisioner.migrate(migration, project, [uuid4()], source, target))
    assert result["status"] == "copied"
    pod = next(item for item in created if item.get("kind") == "Pod")
    compile(pod["spec"]["containers"][0]["command"][2], "<tenant-migration>", "exec")
    claims = {volume["persistentVolumeClaim"]["claimName"] for volume in pod["spec"]["volumes"]
              if "persistentVolumeClaim" in volume}
    assert claims == {generation_claim(source), generation_claim(target)}


def test_generation_storage_measurement_mounts_only_target_read_only():
    tenant = uuid4()
    provisioner = Provisioner(config())
    created = []

    async def kube(method, resource, name="", body=None, text=False, **kwargs):
        if method == "GET" and resource == "persistentvolumeclaims":
            return {"spec": {"resources": {"requests": {"storage": "20Gi"}}}}
        if method == "POST":
            created.append(body)
            return body
        if method == "GET" and resource == "pods" and name.endswith("/log"):
            return json.dumps({"used_bytes": 123, "capacity_bytes": 1000, "available_bytes": 700})
        if method == "GET" and resource == "pods":
            return {"status": {"phase": "Succeeded"}}
        return {}

    provisioner.kube = kube
    result = asyncio.run(provisioner.measure_storage(tenant))
    assert result["used_bytes"] == 123
    pod = created[0]
    volumes = [item for item in pod["spec"]["volumes"] if "persistentVolumeClaim" in item]
    assert volumes == [{"name": "storage", "persistentVolumeClaim": {
        "claimName": generation_claim(tenant), "readOnly": True}}]
    mount = pod["spec"]["containers"][0]["volumeMounts"][0]
    assert mount["readOnly"] is True and pod["spec"]["automountServiceAccountToken"] is False


def test_controller_dispatches_and_preserves_default_provider():
    app = create_controller(config())
    calls = []
    dispatched = Event()

    async def relay(user, method, path, body=None, **kwargs):
        return {"generator": "codex", "status": "connected"}

    async def wait_and_start(user, tenant, body, route):
        calls.append(("/jobs", route))
        dispatched.set()
        return {"status": "generating"}

    app.state.provisioner.relay = relay
    app.state.provisioner.wait_and_start = wait_and_start
    user, tenant = uuid4(), uuid4()
    with TestClient(app, headers={"Authorization": "Bearer " + TOKEN}) as client:
        assert client.get(f"/users/{user}/account").json()["generator"] == "gemini"
        from backend.tests.test_resumption import SPEC
        for provider, route in [("codex", "codex"), ("gemini", "gemini"), (None, "gemini")]:
            payload = GenerationInput(job_id=uuid4(), project_id=uuid4(), specification=SPEC, generator=provider)
            result = client.post(f"/tenants/{tenant}/users/{user}/jobs/start", json=payload.model_dump(mode="json"))
            assert result.status_code == 200 and result.json()["status"] == "starting"
            assert dispatched.wait(1)
            assert calls[-1] == ("/jobs", route)
            dispatched.clear()


def test_controller_can_cancel_while_worker_pod_is_starting():
    app = create_controller(config())
    started = Event()

    async def wait_and_start(user, tenant, body, route):
        started.set()
        await asyncio.sleep(60)

    async def relay(*args, **kwargs):
        raise HTTPException(503, "not ready")

    app.state.provisioner.wait_and_start = wait_and_start
    app.state.provisioner.relay = relay
    user, tenant, job = uuid4(), uuid4(), uuid4()
    from backend.tests.test_resumption import SPEC
    payload = GenerationInput(job_id=job, project_id=uuid4(), specification=SPEC, generator="gemini")
    with TestClient(app, headers={"Authorization": "Bearer " + TOKEN}) as client:
        result = client.post(f"/tenants/{tenant}/users/{user}/jobs/start", json=payload.model_dump(mode="json"))
        assert result.json() == {"status": "starting", "generator": "gemini"}
        assert started.wait(1)
        stopped = client.post(f"/tenants/{tenant}/users/{user}/jobs/{job}/cancel")
        assert stopped.json() == {"status": "failed", "failure_code": "cancelled"}
        assert app.state.pending_jobs == {}


def test_controller_routes_gemini_interview_to_gemini_worker():
    app = create_controller(config())
    calls = []

    async def kube(method, resource, name="", body=None):
        return None

    async def wait_and_relay(user, tenant, method, path, body, route):
        calls.append((method, path, route, body["name"]))
        return {"status": "starting", "provider": route}

    app.state.provisioner.kube = kube
    app.state.provisioner.wait_and_relay = wait_and_relay
    from backend.tests.test_resumption import SPEC
    user, tenant, project = uuid4(), uuid4(), uuid4()
    with TestClient(app, headers={"Authorization": "Bearer " + TOKEN}) as client:
        idle = client.get(f"/tenants/{tenant}/users/{user}/projects/{project}/interview?provider=gemini")
        assert idle.json()["status"] == "idle" and idle.json()["provider"] == "gemini"
        started = client.post(f"/tenants/{tenant}/users/{user}/projects/{project}/interview?provider=gemini",
                              json=SPEC.model_dump(mode="json"))
        assert started.json() == {"status": "starting", "provider": "gemini"}
    assert calls == [("POST", f"/projects/{project}/interview?provider=gemini",
                      "gemini", SPEC.name)]


@pytest.mark.parametrize("suffix,method", [("", "GET"), ("/progress", "GET"), ("/bundle", "GET"), ("/cancel", "POST")])
def test_job_requests_reach_recorded_worker(monkeypatch, suffix, method):
    user, tenant, job = uuid4(), uuid4(), uuid4()
    provisioner = Provisioner(config())
    requests = []

    async def kube(verb, resource, name="", body=None):
        if resource == "pods":
            return {"status": {"podIP": "10.42.0.9",
                               "conditions": [{"type": "Ready", "status": "True"}]}}
        return {"data": {"token": base64.b64encode(TOKEN.encode()).decode()}}

    def handler(request):
        requests.append((request.url.host, request.url.path))
        return httpx.Response(200, json={"generator": "gemini", "status": "generating"})

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    provisioner.kube = kube
    asyncio.run(provisioner.relay(user, method, f"/jobs/{job}{suffix}", tenant=tenant))
    assert requests[-1][0] == "10.42.0.9"
    assert requests[-1][1] == f"/jobs/{job}{suffix}"


def test_job_status_reaches_the_shared_worker_without_provider_lookup(monkeypatch):
    user, tenant, job = uuid4(), uuid4(), uuid4()
    provisioner = Provisioner(config())
    requests = []

    async def kube(verb, resource, name="", body=None):
        if resource == "pods":
            return {"status": {"podIP": "10.42.0.10",
                               "conditions": [{"type": "Ready", "status": "True"}]}}
        return {"data": {"token": base64.b64encode(TOKEN.encode()).decode()}}

    def handler(request):
        requests.append((request.url.host, request.url.path))
        return httpx.Response(200, json={"generator": "gemini", "status": "generating"})

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    provisioner.kube = kube
    result = asyncio.run(provisioner.relay(user, "GET", f"/jobs/{job}", tenant=tenant))
    assert result["status"] == "generating"
    assert [path for _, path in requests] == [f"/jobs/{job}"]


def test_an_unknown_job_is_reported_as_unknown_not_as_unreachable(monkeypatch):
    """起動しているワーカーが「知らない」と答えたなら、それは確かな答え。

    これを503にすると、届かなかっただけの場合と区別がつかなくなる。
    """
    user, tenant, job = uuid4(), uuid4(), uuid4()
    provisioner = Provisioner(config())

    async def kube(verb, resource, name="", body=None):
        if resource == "pods":
            return {"status": {"podIP": "10.42.0.11",
                               "conditions": [{"type": "Ready", "status": "True"}]}}
        return {"data": {"token": base64.b64encode(TOKEN.encode()).decode()}}

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(
        transport=httpx.MockTransport(lambda request: httpx.Response(404, json={"detail": "no"})), **kw))
    provisioner.kube = kube
    with pytest.raises(HTTPException) as raised:
        asyncio.run(provisioner.relay(user, "GET", f"/jobs/{job}", tenant=tenant))
    assert raised.value.status_code == 404


def test_shared_worker_accepts_provider_switch_and_never_repeats_a_lost_job(tmp_path):
    worker = Agent(AgentSettings(_env_file=None, token=TOKEN, user_id=uuid4(),
                                 root=tmp_path, generator="gemini"))
    project, other = uuid4(), uuid4()
    lock = worker.acquire_storage_lock(project)
    try:
        with pytest.raises(HTTPException) as exc:
            worker.acquire_storage_lock(project)
        assert exc.value.status_code == 409
        with worker.acquire_storage_lock(other):
            pass
    finally:
        lock.close()
    with worker.acquire_storage_lock(project):
        pass
    job = uuid4()
    worker.active_job = str(job)
    worker.active_provider = "gemini"
    worker.write_status(job, "generating")
    assert worker.job_status(job)["generator"] == "gemini"
    worker.active_job = None
    assert worker.job_status(job)["status"] == "failed"  # restart never repeats inference


def test_gemini_start_needs_no_codex_login_and_releases_shared_lock(tmp_path):
    from backend.tests.test_resumption import SPEC
    agent = Agent(AgentSettings(_env_file=None, token=TOKEN, user_id=uuid4(), root=tmp_path, generator="gemini"))
    payload = GenerationInput(job_id=uuid4(), project_id=uuid4(), specification=SPEC)
    async def account(*args, **kwargs):
        raise AssertionError("Gemini must not invoke Codex")
    agent.account = account
    async def turn(payload):
        try:
            agent.write_status(payload.job_id, "generated")
        finally:
            agent.active_job = None
            agent.release_storage_lock()
    agent.generate = turn
    async def run():
        assert (await agent.start_generation(payload))["status"] == "generating"
        await agent.task
        assert (await agent.start_generation(payload))["status"] == "generated"
        with agent.acquire_storage_lock(payload.project_id):
            pass
    asyncio.run(run())


def test_gemini_interview_asks_then_returns_project(tmp_path, monkeypatch):
    """1回で切り上げず、聞くことが無くなるまで続ける。上限まで来たら仕上げる。"""
    from backend.tests.test_resumption import SPEC
    calls = []

    async def interview(model, specification, answers=None, final=False):
        calls.append(answers)
        if answers is None:
            return GeminiInterviewDecision(action="ask", questions=[{
                "id": "approval", "header": "承認", "question": "登録後に承認しますか？",
                "options": [
                    {"label": "承認する（推奨）", "description": "担当者が内容を確認します。"},
                    {"label": "承認しない", "description": "登録時点で確定します。"}],
                "is_other": False}])
        if final:
            return ProjectInput(**{**SPEC.model_dump(), "requirements": ["登録後に担当者が承認する。"]})
        # 2回目は聞くことが無い。そこで打ち切り、仕上げへ進む。
        return GeminiInterviewDecision(
            action="complete",
            result=ProjectInput(**{**SPEC.model_dump(), "requirements": ["登録後に担当者が承認する。"]}))

    monkeypatch.setattr("backend.worker.agent.structured_interview", interview)
    agent = Agent(AgentSettings(_env_file=None, token=TOKEN, user_id=uuid4(), root=tmp_path,
                                generator="gemini", gemini_model="gemini-3.5-flash"))
    project_id = uuid4()

    async def run():
        await agent.start_interview(project_id, SPEC)
        for _ in range(20):
            await asyncio.sleep(0)
            state = agent.interview_status(project_id)
            if state["status"] == "waiting":
                break
        assert state["provider"] == "gemini" and state["questions"][0]["id"] == "approval"
        await agent.answer_interview(project_id,
                                     type("Answer", (), {"answers": {"approval": ["承認する（推奨）"]}})())
        await agent.task
        state = agent.interview_status(project_id)
        assert state["status"] == "completed"
        assert state["result"]["requirements"] == ["登録後に担当者が承認する。"]
        # 2回目の呼び出しには、1回目の質問と回答が積まれて渡る。
        assert len(calls) == 2 and calls[1][0]["answers"]["approval"] == ["承認する（推奨）"]

    asyncio.run(run())


def test_legacy_history_defaults_to_codex_without_modification(tmp_path):
    settings = AgentSettings(_env_file=None, token=TOKEN, user_id=uuid4(), root=tmp_path)
    app = create_agent(settings)
    job = uuid4()
    folder = app.state.agent.job_path(job)
    folder.mkdir(parents=True)
    path = folder / "status.json"
    original = json.dumps({"status": "generated"})
    path.write_text(original)
    with TestClient(app, headers={"Authorization": "Bearer " + TOKEN}) as client:
        assert client.get(f"/jobs/{job}/route").json() == {"generator": "codex"}
    assert path.read_text() == original


def test_storage_lock_is_released_when_startup_cannot_write(tmp_path, monkeypatch):
    from backend.tests.test_resumption import SPEC
    agent = Agent(AgentSettings(_env_file=None, token=TOKEN, user_id=uuid4(), root=tmp_path, generator="gemini"))
    payload = GenerationInput(job_id=uuid4(), project_id=uuid4(), specification=SPEC)
    def failed_write(*args, **kwargs):
        raise OSError("storage unavailable")
    monkeypatch.setattr(agent, "write_status", failed_write)
    with pytest.raises(OSError):
        asyncio.run(agent.start_generation(payload))
    assert agent.active_job is None and agent.generation_lock is None
    with agent.acquire_storage_lock(payload.project_id):
        pass


def config_with(**overrides):
    base = {"token": "x" * 40,
            "agent_image": "registry.example.com/koyorina-agent@sha256:" + "a" * 64}
    return ControllerSettings(**{**base, **overrides})


def agent_pod(user, route, ready=True):
    return {"metadata": {"name": f"{route}-{user.hex}",
                         "labels": {"forge-user": str(user), "forge-route": route}},
            "status": {"conditions": [{"type": "Ready", "status": "True" if ready else "False"}]}}


def reaper(states, settings=None, pods=None):
    """/runtime の応答を差し替えて、回収の判断だけを見る。

    差し替える kube は本物と同じ受け口にする。**kwargs で何でも受けると、
    実際には存在しない引数で呼んでいても気づけない（それで回収処理が
    一度も動かないまま、2分ごとに TypeError を出し続けていた）。
    """
    provisioner = Provisioner(settings or config_with(idle_minutes=30))
    deleted = []
    deleted_services = []
    signature = inspect.signature(Provisioner.kube)

    async def kube(verb, resource, name="", body=None, query=""):
        # 本物の kube が受け取れる形でしか呼ばれていないことを確かめる。
        signature.bind(provisioner, verb, resource, name=name, body=body, query=query)
        if resource == "pods" and verb == "DELETE":
            deleted.append(name)
            return {}
        if resource == "services" and verb == "DELETE":
            deleted_services.append(name)
            return {}
        if resource == "pods":
            return {"items": pods}
        return {}

    async def relay(user_id, method, path, body=None, **kwargs):
        return states[kwargs.get("route")]

    provisioner.kube, provisioner.relay = kube, relay
    provisioner.deleted_services = deleted_services
    return provisioner, deleted


def test_a_pod_nobody_has_used_is_taken_back(monkeypatch):
    """使われていないPodが残り続けると、触ってもいない人のぶんまで枠を押さえる。"""
    user = uuid4()
    provisioner, deleted = reaper(
        {"gemini": {"busy": False, "idle_seconds": 3600}},
        pods=[agent_pod(user, "gemini")])
    assert asyncio.run(provisioner.reap_idle()) == [f"gemini-{user.hex}"]
    assert deleted == [f"gemini-{user.hex}"]
    assert provisioner.deleted_services == [f"gemini-{user.hex}"]


def test_old_worker_services_without_pods_are_taken_back():
    provisioner = Provisioner(config_with(idle_minutes=30))
    deleted = []

    async def kube(verb, resource, name="", body=None, query=""):
        if verb == "GET" and resource == "pods":
            return {"items": [{"metadata": {"name": "still-running"}}]}
        if verb == "GET" and resource == "services":
            return {"items": [
                {"metadata": {"name": "still-running",
                              "creationTimestamp": "2020-01-01T00:00:00Z"}},
                {"metadata": {"name": "old-worker",
                              "creationTimestamp": "2020-01-01T00:00:00Z"}},
            ]}
        if verb == "DELETE" and resource == "services":
            deleted.append(name)
            return {}
        return None

    provisioner.kube = kube
    assert asyncio.run(provisioner.reap_orphan_services()) == ["old-worker"]
    assert deleted == ["old-worker"]


def test_work_in_progress_is_never_taken_back():
    """生成中・ログイン中は消さない。配備での入れ替えと同じ判断を使う。"""
    user = uuid4()
    for state in ({"busy": True, "idle_seconds": 99999},
                  {"busy": False, "idle_seconds": 60}):
        provisioner, deleted = reaper({"gemini": state}, pods=[agent_pod(user, "gemini")])
        assert asyncio.run(provisioner.reap_idle()) == []
        assert not deleted


def test_a_pod_whose_state_is_unknown_is_left_alone():
    """確かめられないものは消さない。起動途中かもしれない。"""
    user = uuid4()
    provisioner, deleted = reaper({}, pods=[agent_pod(user, "gemini", ready=False)])
    assert asyncio.run(provisioner.reap_idle()) == []

    broken, gone = reaper({"gemini": {}}, pods=[agent_pod(user, "gemini")])

    async def refuse(*args, **kwargs):
        raise HTTPException(503, "unreachable")

    broken.relay = refuse
    assert asyncio.run(broken.reap_idle()) == []
    assert not gone


def test_reaping_can_be_switched_off():
    """止めたい環境では 0 にする。設定を読まずに回収が始まらないこと。"""
    user = uuid4()
    provisioner, deleted = reaper({"gemini": {"busy": False, "idle_seconds": 99999}},
                                  settings=config_with(idle_minutes=0),
                                  pods=[agent_pod(user, "gemini")])
    assert asyncio.run(provisioner.reap_idle()) == []
    assert not deleted


@pytest.mark.parametrize("method,path,suffix", [
    ("GET", "/history", "/history?limit=50"),
    ("GET", "/history/0643e95", "/history/0643e95"),
    ("POST", "/history/0643e95/restore", "/history/0643e95/restore"),
    ("DELETE", "/history", "/history"),
])
def test_the_change_history_is_relayed_to_a_running_worker(method, path, suffix):
    """画面とエージェントだけ作ってあり、間の中継が無かった。

    管理側の要求が404で返り、画面には「まだ記録がありません」とだけ出ていた。
    実体はアプリ単位の共有領域にあるので、動いているワーカーならどれでもよい。
    """
    user, tenant, project = uuid4(), uuid4(), uuid4()
    calls = []

    async def relay(user_id, verb, target, body=None, **kwargs):
        calls.append((verb, target, kwargs.get("route")))
        return {"entries": []}

    settings = config_with(generator="gemini", vertex_project="p")
    app = create_controller(settings)
    app.state.provisioner.relay = relay

    async def kube(verb, resource, name="", body=None, **kwargs):
        # geminiだけが起動している状態。codexのPodは作らない。
        if resource == "pods" and name.startswith("gemini-"):
            return {"status": {"conditions": [{"type": "Ready", "status": "True"}]}}
        return None

    app.state.provisioner.kube = kube
    with TestClient(app) as client:
        response = client.request(method, f"/tenants/{tenant}/users/{user}/projects/{project}{path}",
                                  headers={"Authorization": "Bearer " + settings.token.get_secret_value()},
                                  json={} if method == "POST" else None)
    assert response.status_code == 200, response.text
    assert calls == [(method, f"/projects/{project}{suffix}", None)]


@pytest.mark.parametrize("state", ["active_job", "interview_request", "login"])
def test_a_pod_in_use_is_never_reported_idle(state):
    """止めてよいかの判断は1か所に集める。

    /runtime だけヒアリングを数え落としていた。答えを待っている間は要求が
    来ないので、考えている人のPodを配備やアイドル回収が消してしまう。
    記録は残るが、モデルとのやり取りは失われ、最初からになる。
    """
    settings = AgentSettings(token="x" * 40, user_id=str(uuid4()))
    app = create_agent(settings)
    agent = app.state.agent          # ルートはこの実体を見る。差し替えない。
    assert agent.in_use() is False
    setattr(agent, state, {"any": "value"})
    assert agent.in_use() is True

    with TestClient(app) as client:
        response = client.get("/runtime", headers={
            "Authorization": "Bearer " + settings.token.get_secret_value()})
    assert response.json()["busy"] is True
