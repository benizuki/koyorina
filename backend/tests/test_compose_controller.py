"""Docker Compose版のcontroller。k8s APIに一切触れず、常駐エージェント1つへ中継する。"""
import asyncio
import json
import os
from unittest.mock import patch
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from backend.config.settings import Settings
from backend.worker import controller
from backend.worker.controller import ComposeProvisioner, ControllerSettings, Provisioner

AGENT_TOKEN = "a" * 48
TENANT = "00000000-0000-4000-8000-000000000001"


def compose_settings(**overrides):
    values = {"token": "c" * 48, "backend": "compose", "agent_token": AGENT_TOKEN, **overrides}
    with patch.dict(os.environ, {}, clear=True):
        return ControllerSettings(_env_file=None, **values)


@pytest.fixture
def agent(monkeypatch):
    """compose網のエージェントの代わり。届いた要求を記録する。"""
    seen = []

    def respond(request):
        seen.append(request)
        if request.url.path == "/healthz":
            return httpx.Response(200, json={"status": "ok"})
        return httpx.Response(200, json={"status": "queued"})

    real = httpx.AsyncClient
    monkeypatch.setattr(controller.httpx, "AsyncClient",
                        lambda **kwargs: real(transport=httpx.MockTransport(respond),
                                              **{k: v for k, v in kwargs.items() if k != "verify"}))

    async def no_kubernetes(*args, **kwargs):
        raise AssertionError("compose版はKubernetes APIを呼ばない")
    monkeypatch.setattr(Provisioner, "kube", no_kubernetes)
    return seen


def test_compose_needs_no_agent_image_but_kubernetes_still_pins_a_digest():
    assert compose_settings().agent_image == ""
    with pytest.raises(ValidationError):
        with patch.dict(os.environ, {}, clear=True):
            ControllerSettings(_env_file=None, token="c" * 48, agent_image="registry.example.com/koyorina-agent:latest")


def test_compose_requires_the_agent_token():
    with pytest.raises(ValidationError):
        compose_settings(agent_token="")


def test_agent_url_must_be_a_plain_compose_service():
    for url in ("http://agent:8080/x", "https://agent:8080", "http://user@agent:8080"):
        with pytest.raises(ValidationError):
            compose_settings(agent_url=url)


def test_a_job_is_relayed_to_the_resident_agent_with_its_token(agent):
    provisioner = ComposeProvisioner(compose_settings())
    user = uuid4()
    result = asyncio.run(provisioner.wait_and_start(user, TENANT, {"job_id": str(uuid4())}, "codex"))
    assert result == {"status": "queued"}
    request = agent[-1]
    assert (request.method, str(request.url)) == ("POST", "http://agent:8080/jobs")
    assert request.headers["authorization"] == "Bearer " + AGENT_TOKEN


def test_login_and_account_reach_the_same_agent(agent):
    provisioner = ComposeProvisioner(compose_settings())
    asyncio.run(provisioner.relay(uuid4(), "POST", "/login", prepare=True, auth_only=True))
    asyncio.run(provisioner.relay(uuid4(), "GET", "/account", auth_only=True))
    assert [str(r.url) for r in agent] == ["http://agent:8080/login", "http://agent:8080/account"]


def test_runtime_status_follows_the_agent_health(agent):
    status = asyncio.run(ComposeProvisioner(compose_settings()).runtime_status(uuid4()))
    assert status["codex"]["state"] == "running"


@pytest.mark.parametrize("call", [
    lambda p: p.measure_storage(TENANT),
    lambda p: p.migrate(uuid4(), uuid4(), [], TENANT, TENANT),
])
def test_operations_that_need_kubernetes_are_refused_not_ignored(agent, call, tmp_path):
    with pytest.raises(HTTPException) as refused:
        asyncio.run(call(ComposeProvisioner(compose_settings(settings_dir=tmp_path))))
    assert refused.value.status_code == 409


def saved(provisioner, coroutine):
    """保存し、k8s版ならPodを作り直すところで書かれる環境を待って読む。"""
    async def run():
        await coroutine
        await asyncio.gather(*getattr(provisioner, "_retire_tasks", set()))
    asyncio.run(run())
    return json.loads((provisioner.settings_dir / "agent-env.json").read_text())


def test_ui_settings_reach_the_agent_environment_with_the_key(agent, tmp_path):
    from backend.worker.controller import SystemGeminiInput, SystemOpenAICompatibleInput
    provisioner = ComposeProvisioner(compose_settings(settings_dir=tmp_path))
    environment = saved(provisioner, provisioner.set_system_llm("openai-compatible", SystemOpenAICompatibleInput(
        enabled=True, base_url="http://host.docker.internal:11434/v1", model="gemma4", api_key="ollama-key")))
    assert environment["AGENT_OPENAI_COMPATIBLE_ENABLED"] == "true"
    assert environment["AGENT_OPENAI_COMPATIBLE_MODEL"] == "gemma4"
    assert environment["AGENT_OPENAI_COMPATIBLE_API_KEY"] == "ollama-key"
    environment = saved(provisioner, provisioner.set_system_gemini(SystemGeminiInput(
        backend="developer", model="gemini-3.8-flash", api_key="gemini-key")))
    assert environment["GEMINI_API_KEY"] == "gemini-key"
    assert environment["AGENT_OPENAI_COMPATIBLE_API_KEY"] == "ollama-key"  # 別の種類の設定は残る
    # composeが決めた身元は、画面の設定で上書きさせない。
    assert not {"AGENT_USER_ID", "AGENT_TOKEN", "AGENT_TENANT_ID"} & set(environment)
    for name in ("store.json", "agent-env.json"):
        assert (tmp_path / name).stat().st_mode & 0o777 == 0o600


def test_disabling_a_provider_in_the_ui_removes_it_from_the_agent(agent, tmp_path):
    from backend.worker.controller import SystemOpenAICompatibleInput
    provisioner = ComposeProvisioner(compose_settings(settings_dir=tmp_path))
    saved(provisioner, provisioner.set_system_llm("openai-compatible", SystemOpenAICompatibleInput(
        enabled=True, base_url="http://ollama:11434/v1", model="m", api_key="k")))
    environment = saved(provisioner, provisioner.set_system_llm(
        "openai-compatible", SystemOpenAICompatibleInput(enabled=None)))
    assert "AGENT_OPENAI_COMPATIBLE_API_KEY" not in environment


def test_vertex_is_refused_in_compose(agent, tmp_path):
    from backend.worker.controller import SystemGeminiInput
    provisioner = ComposeProvisioner(compose_settings(settings_dir=tmp_path))
    with pytest.raises(HTTPException) as refused:
        asyncio.run(provisioner.set_system_gemini(SystemGeminiInput(
            backend="vertex", project="example-project", location="global")))
    assert refused.value.status_code == 409


def test_the_launcher_restarts_the_agent_only_when_idle(tmp_path, monkeypatch):
    from backend.worker import compose_agent
    settings = tmp_path / "agent-env.json"
    monkeypatch.setattr(compose_agent, "SETTINGS", settings)
    monkeypatch.setenv("KEPT", "compose")
    settings.write_text(json.dumps({"GEMINI_API_KEY": "from-ui", "KEPT": "ui"}))
    environment = compose_agent.environment()
    assert environment["GEMINI_API_KEY"] == "from-ui" and environment["KEPT"] == "ui"
    started, stopped = [], []
    busy = iter([True, False])

    class Child:
        returncode = 0

        def __init__(self, command, env):
            started.append(env.get("GEMINI_API_KEY"))
        def poll(self):
            return None if len(started) < 2 else 0

    monkeypatch.setattr(compose_agent.subprocess, "Popen", Child)
    monkeypatch.setattr(compose_agent, "stop", lambda child: stopped.append(child))
    monkeypatch.setattr(compose_agent, "busy", lambda: next(busy))
    monkeypatch.setattr(compose_agent.signal, "signal", lambda *args: None)
    ticks = iter(range(10))

    def tick(_seconds):
        step = next(ticks)
        if step == 0:
            settings.write_text(json.dumps({"GEMINI_API_KEY": "changed"}))
    monkeypatch.setattr(compose_agent.time, "sleep", tick)
    with pytest.raises(SystemExit):
        compose_agent.main()
    # 1回目の確認は生成中なので待ち、手すきになってから1度だけ起動し直す。
    assert started == ["from-ui", "changed"] and len(stopped) == 1


def test_the_management_app_accepts_the_compose_controller_only_locally():
    base = dict(database_url="postgresql+psycopg://unused/unused",
                codex_controller_url="http://codex-controller:8080", codex_controller_token="c" * 40)
    with patch.dict(os.environ, {}, clear=True):
        assert Settings(_env_file=None, app_env="local", app_origin="http://localhost:8080", **base)
        with pytest.raises(ValidationError):
            Settings(_env_file=None, app_env="production", app_origin="https://example.test",
                     app_session_secret="s" * 40, google_oauth_client_id="client", **base)


def test_a_compose_preview_joins_the_network_instead_of_publishing(tmp_path, monkeypatch):
    from backend.core import preview_runtime
    from backend.domain.preview import PreviewPaths
    calls = []

    async def docker(*args, environment=None, timeout=60):
        calls.append(args)
        return 0, "", ""
    monkeypatch.setattr(preview_runtime, "docker", docker)
    project = uuid4()
    paths = PreviewPaths(tmp_path, project).prepare()
    asyncio.run(preview_runtime.run(project, paths, 8101, "preview:local", {}, network="koyorina_default"))
    run = next(args for args in calls if args[0] == "run")
    assert "--publish" not in run
    assert run[run.index("--network") + 1] == "koyorina_default"
    asyncio.run(preview_runtime.run(project, paths, 8101, "preview:local", {}))
    run = [args for args in calls if args[0] == "run"][-1]
    assert "--network" not in run and "127.0.0.1:8101:8080" in run


def test_a_compose_preview_is_reached_by_container_name(tmp_path):
    from backend.core.preview_backend import DockerBackend
    from backend.core.preview_runtime import container_name
    from backend.domain.preview import write_state
    with patch.dict(os.environ, {}, clear=True):
        settings = Settings(_env_file=None, app_env="local", app_origin="http://localhost:8080",
                            database_url="postgresql+psycopg://unused/unused", preview_enabled=True,
                            preview_root=tmp_path, preview_docker_network="koyorina_default")
    backend = DockerBackend(settings)
    project = uuid4()
    write_state(backend.paths(project), {"port": 8101})
    assert backend.target(project) == f"http://{container_name(project)}:8080"
    local = DockerBackend(settings.model_copy(update={"preview_docker_network": ""}))
    assert local.target(project) == "http://127.0.0.1:8101"


def local_settings(**overrides):
    values = dict(app_env="local", app_origin="http://localhost:8080",
                  database_url="postgresql+psycopg://unused/unused", **overrides)
    with patch.dict(os.environ, {}, clear=True):
        return Settings(_env_file=None, **values)


def test_the_compose_gateway_is_trusted_only_locally_and_only_as_a_private_address():
    assert local_settings(local_client_hosts="172.29.80.1").local_client_host_set == {"172.29.80.1"}
    for value in ("8.8.8.8", "not-an-ip"):
        with pytest.raises(ValidationError):
            local_settings(local_client_hosts=value)
    with pytest.raises(ValidationError):
        with patch.dict(os.environ, {}, clear=True):
            Settings(_env_file=None, app_env="production", app_origin="https://example.test",
                     app_session_secret="s" * 40, google_oauth_client_id="client",
                     database_url="postgresql+psycopg://unused/unused", local_client_hosts="172.29.80.1")


@pytest.mark.parametrize("client,allowed", [("172.29.80.1", True), ("127.0.0.1", True),
                                            ("172.30.0.5", False), ("172.29.80.7", False)])
def test_dev_login_accepts_the_gateway_but_not_other_containers(client, allowed):
    from types import SimpleNamespace
    from backend.core.auth import actor
    request = SimpleNamespace(client=SimpleNamespace(host=client), session={},
                              app=SimpleNamespace(state=SimpleNamespace(
                                  settings=local_settings(local_client_hosts="172.29.80.1"))))
    db = SimpleNamespace(scalar=lambda query: SimpleNamespace(id="u", email="local-developer@example.invalid"))
    if allowed:
        assert actor(request, db).id == "u"
    else:
        with pytest.raises(HTTPException) as refused:
            actor(request, db)
        assert refused.value.status_code == 403


def test_loopback_aliases_are_sent_to_the_app_origin_locally_only(tmp_path):
    """127.0.0.1 で開いても使えるように、APP_ORIGIN（localhost）へ送る。本番では送らない。"""
    from fastapi.testclient import TestClient
    from backend.main import create_app
    app = create_app(local_settings())
    with TestClient(app, base_url="http://127.0.0.1:8080") as client:
        moved = client.get("/projects?tab=1", follow_redirects=False)
        assert moved.status_code == 307
        assert moved.headers["location"] == "http://localhost:8080/projects?tab=1"
        ipv6 = client.get("/", headers={"host": "[::1]:8080"}, follow_redirects=False)
        assert ipv6.headers["location"] == "http://localhost:8080/"
        # 他のホスト名は従来どおり断る（転送先を要求で選ばせない）。
        assert client.get("/", headers={"host": "evil.example"}, follow_redirects=False).status_code == 400
