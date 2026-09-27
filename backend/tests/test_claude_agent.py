"""Claude（Claude Agent SDK）での生成。SDK の query を偽物にして、Koyorina 側の約束を確かめる。"""
import asyncio
from types import SimpleNamespace

import pytest

from backend.domain.generation_progress import Progress
from backend.worker import claude_agent
from backend.worker.gemini_agent import ProviderUnavailable, QuotaExceeded


class AssistantMessage:
    def __init__(self, text):
        self.content = [TextBlock(text)]


class TextBlock:
    def __init__(self, text):
        self.text = text


class ResultMessage:
    def __init__(self, is_error=False, status=None, errors=None):
        self.is_error, self.api_error_status, self.errors, self.result = is_error, status, errors or [], ""
        self.usage = {"input_tokens": 10, "cache_read_input_tokens": 90, "cache_creation_input_tokens": 0,
                      "output_tokens": 20}


def fake_query(writes=(), result=None):
    async def query(prompt, options):
        for name, args in writes:
            options["callables"][name](**args)
        yield AssistantMessage("## 作りました")
        yield result or ResultMessage()
    return query


def builder(workspace, model, tools, config_dir, effort=""):
    return {"tools": tools, "callables": {t.__name__: t for t in tools}, "model": model, "effort": effort,
            "config_dir": config_dir}


def test_claude_writes_only_through_koyorina_tools(tmp_path):
    workspace = tmp_path / "w"
    workspace.mkdir()
    progress = Progress(tmp_path, "j")
    usage = asyncio.run(claude_agent.run(
        workspace, prompt="作って", model="claude-sonnet-5", progress=progress,
        query=fake_query([("write_file", {"path": "backend/main.py", "text": "app = 1\n"}),
                          ("write_file", {"path": "../escape.py", "text": "x"})]),
        options_builder=builder))
    assert (workspace / "backend/main.py").read_text() == "app = 1\n"
    assert not (tmp_path / "escape.py").exists()
    assert usage == {"input_tokens": 100, "output_tokens": 20, "cached_tokens": 90, "total_tokens": 120}
    assert [e for e in progress.data["events"] if e["kind"] == "codex"][-1]["message"] == "## 作りました"


@pytest.mark.parametrize("status, errors, expected", [
    (429, ["rate_limit_error"], QuotaExceeded), (529, ["overloaded_error"], ProviderUnavailable)])
def test_provider_errors_become_koyorina_failures(tmp_path, status, errors, expected):
    with pytest.raises(expected):
        asyncio.run(claude_agent.run(tmp_path, prompt="p", model="m", progress=Progress(tmp_path, "j"),
                                     query=fake_query(result=ResultMessage(True, status, errors)),
                                     options_builder=builder))


def test_the_connection_follows_the_settings_and_never_phones_home(monkeypatch, tmp_path):
    for name in ("AGENT_CLAUDE_BACKEND", "AGENT_CLAUDE_API_KEY", "AGENT_CLAUDE_PROJECT",
                 "AGENT_CLAUDE_LOCATION", "GOOGLE_APPLICATION_CREDENTIALS"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(RuntimeError, match="APIキー"):
        claude_agent.endpoint_env(str(tmp_path))
    monkeypatch.setenv("AGENT_CLAUDE_API_KEY", "sk-ant-test-only")
    env = claude_agent.endpoint_env(str(tmp_path))
    assert env["ANTHROPIC_API_KEY"] == "sk-ant-test-only" and "CLAUDE_CODE_USE_VERTEX" not in env
    assert env["DISABLE_TELEMETRY"] == env["DISABLE_ERROR_REPORTING"] == "1"
    assert env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == "1" and env["CLAUDE_CONFIG_DIR"] == str(tmp_path)
    monkeypatch.setenv("AGENT_CLAUDE_BACKEND", "vertex")
    monkeypatch.setenv("AGENT_CLAUDE_PROJECT", "claude-project-01")
    with pytest.raises(RuntimeError, match="Vertex AI"):
        claude_agent.endpoint_env(str(tmp_path))
    (tmp_path / "config.json").write_text("{}")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(tmp_path / "config.json"))
    env = claude_agent.endpoint_env(str(tmp_path))
    assert env["CLAUDE_CODE_USE_VERTEX"] == "1" and env["ANTHROPIC_VERTEX_PROJECT_ID"] == "claude-project-01"
    assert "ANTHROPIC_API_KEY" not in env


def test_the_real_options_disable_builtin_tools(tmp_path, monkeypatch):
    pytest.importorskip("claude_agent_sdk")
    monkeypatch.setenv("AGENT_CLAUDE_API_KEY", "sk-ant-test-only")
    monkeypatch.delenv("AGENT_CLAUDE_BACKEND", raising=False)
    from backend.worker.gemini_agent import sdk_tools
    tools = sdk_tools(tmp_path, Progress(tmp_path, "j"))
    options = claude_agent.build_options(tmp_path, "claude-sonnet-5", tools, str(tmp_path), "high")
    assert options.tools == [] and options.setting_sources == []
    assert sorted(options.allowed_tools) == sorted(f"mcp__koyorina__{t.__name__}" for t in tools)
    assert options.effort == "high" and options.model == "claude-sonnet-5"
