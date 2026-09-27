"""Gemini でのコード生成。中身は google-antigravity SDK（生成エージェントの Pod の中）。

SDK そのものは生成エージェントのイメージにだけ入る。ここでは SDK の Agent を偽物に差し替え、
Koyorina 側の約束（道具は作業場所の中だけ、コマンドは渡さない、失敗の読み替え、報告の記録）を確かめる。
"""
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.domain.generation_progress import Progress
from backend.worker import antigravity_sdk_agent as sdk
from backend.worker.gemini_agent import ProviderUnavailable, QuotaExceeded


class Text:
    def __init__(self, text):
        self.text = text


def fake_agent(script, error=None):
    """config.tools を使ってファイルを書き、文章を流す偽の Agent。"""
    class FakeAgent:
        def __init__(self, config):
            self.config = config

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def chat(self, prompt):
            tools = {tool.__name__: tool for tool in self.config["tools"]}
            config = self.config

            async def chunks():
                for name, args in script:
                    tools[name](**args)
                    yield SimpleNamespace(name=name, args=args)
                if error:
                    raise error
                yield Text("## 作りました\n")
                yield Text("- 一覧画面\n")
            config["prompt"] = prompt
            return SimpleNamespace(chunks=chunks(), usage_metadata=SimpleNamespace(
                prompt_token_count=100, candidates_token_count=20, cached_content_token_count=40,
                total_token_count=120))
    return FakeAgent


def builder(workspace, model, tools, state_dir, thinking_level=""):
    return {"workspace": workspace, "model": model, "tools": tools, "state_dir": state_dir,
            "thinking_level": thinking_level}


def test_the_sdk_writes_only_through_koyorina_tools_and_reports(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    progress = Progress(tmp_path, "job")
    agent = fake_agent([("write_file", {"path": "backend/main.py", "text": "app = 1\n"}),
                        ("write_file", {"path": "../escape.py", "text": "x"})])
    usage = asyncio.run(sdk.run(workspace, prompt="作って", model="gemini-3.8-flash", progress=progress, agent_factory=agent, config_builder=builder))
    assert (workspace / "backend/main.py").read_text() == "app = 1\n"
    # 作業場所の外へは、Koyorina の道具が断る。
    assert not (tmp_path / "escape.py").exists()
    assert usage == {"input_tokens": 100, "output_tokens": 20, "cached_tokens": 40, "total_tokens": 120}
    said = [e for e in progress.data["events"] if e["kind"] == "codex"]
    assert said and said[-1]["message"] == "## 作りました\n- 一覧画面"


def test_commands_are_not_offered_to_the_model(tmp_path):
    captured = {}

    def capture(workspace, model, tools, state_dir, thinking_level=""):
        captured["names"] = sorted(tool.__name__ for tool in tools)
        return builder(workspace, model, tools, state_dir, thinking_level)
    asyncio.run(sdk.run(tmp_path, prompt="p", model="m", progress=Progress(tmp_path, "j"),
                        agent_factory=fake_agent([]), config_builder=capture))
    assert captured["names"] == ["check_files", "list_files", "read_file", "write_file"]


@pytest.mark.parametrize("message, expected", [
    ("Error 429, Message: You exceeded your current quota ... RESOURCE_EXHAUSTED", QuotaExceeded),
    ("Error 503, Message: This model is currently experiencing high demand ... UNAVAILABLE", ProviderUnavailable),
])
def test_provider_errors_become_koyorina_failures(tmp_path, message, expected):
    with pytest.raises(expected):
        asyncio.run(sdk.run(tmp_path, prompt="p", model="m",
                            progress=Progress(tmp_path, "j"),
                            agent_factory=fake_agent([], error=RuntimeError(message)), config_builder=builder))


def test_the_conversation_record_does_not_outlive_the_turn(tmp_path):
    seen = {}

    def capture(workspace, model, tools, state_dir, thinking_level=""):
        seen["dir"] = Path(state_dir)
        assert seen["dir"].is_dir()
        return builder(workspace, model, tools, state_dir, thinking_level)
    asyncio.run(sdk.run(tmp_path, prompt="p", model="m", progress=Progress(tmp_path, "j"),
                        agent_factory=fake_agent([]), config_builder=capture))
    assert not seen["dir"].exists()


def test_the_connection_follows_the_system_gemini_setting(monkeypatch, tmp_path):
    for name in ("GEMINI_API_BACKEND", "GOOGLE_CLOUD_PROJECT", "GOOGLE_APPLICATION_CREDENTIALS",
                 "GEMINI_API_KEY", "GOOGLE_CLOUD_LOCATION"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GEMINI_API_BACKEND", "developer")
    with pytest.raises(RuntimeError, match="APIキー"):
        sdk.endpoint()
    monkeypatch.setenv("GEMINI_API_KEY", "shared-key")
    assert sdk.endpoint() == {"kind": "developer", "api_key": "shared-key"}
    monkeypatch.setenv("GEMINI_API_BACKEND", "vertex")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "example-project-123")
    # 鍵ファイルも WIF の設定も無い（GCE で WIF へ移る前など）。分かるように止める。
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(tmp_path / "missing.json"))
    with pytest.raises(RuntimeError, match="Workload Identity"):
        sdk.endpoint()
    (tmp_path / "credential-config.json").write_text("{}")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(tmp_path / "credential-config.json"))
    assert sdk.endpoint() == {"kind": "vertex", "project": "example-project-123", "location": "global"}


def test_the_agent_entry_builds_the_request_and_passes_the_thinking_level(tmp_path):
    from backend.domain.projects import ProjectInput
    from backend.tests.test_codex_generation import INPUT
    seen = {}

    def capture(workspace, model, tools, state_dir, thinking_level=""):
        seen["level"] = thinking_level
        return builder(workspace, model, tools, state_dir, thinking_level)
    agent = fake_agent([])
    asyncio.run(sdk.gemini_turn(tmp_path, "gemini-3.8-flash", ProjectInput(**INPUT), None,
                                Progress(tmp_path, "j"), thinking_level="high", notes="\nNOTE",
                                agent_factory=agent, config_builder=capture))
    assert seen["level"] == "high"
    # 修正ターンは渡した依頼文をそのまま使う（仕様で包み直さない）。
    captured = {}

    class Recording(agent):
        async def chat(self, prompt):
            captured["prompt"] = prompt
            return await super().chat(prompt)
    asyncio.run(sdk.gemini_turn(tmp_path, "m", ProjectInput(**INPUT), None, Progress(tmp_path, "j"),
                                text="直して", agent_factory=Recording, config_builder=builder))
    assert captured["prompt"] == "直して"


def test_the_real_config_turns_off_tracing_and_builtin_tools(tmp_path, monkeypatch):
    antigravity = pytest.importorskip("google.antigravity")
    monkeypatch.setenv("GEMINI_API_BACKEND", "developer")
    monkeypatch.setenv("GEMINI_API_KEY", "test-only")
    from backend.worker.gemini_agent import sdk_tools
    tools = sdk_tools(tmp_path, Progress(tmp_path, "j"))
    config = sdk.build_config(tmp_path, "gemini-3.8-flash", tools, str(tmp_path), "high")
    assert config.debug_config.enable_server_side_tracing is False
    assert config.capabilities.enabled_tools == [antigravity.BuiltinTools.FINISH]
    text = next(m for m in config.models if m.name == "gemini-3.8-flash")
    assert text.endpoint.options.thinking_level.value == "high"
