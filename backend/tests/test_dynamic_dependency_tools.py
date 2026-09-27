import asyncio
import inspect
from uuid import uuid4

from backend.core.codex_bridge import CodexBridge
from backend.worker.gemini_agent import sdk_tools


class Progress:
    def record(self, *args, **kwargs):
        pass


def test_codex_dynamic_tool_returns_the_app_server_response_shape(tmp_path):
    bridge = CodexBridge("codex", tmp_path, str(uuid4()), allow_generation=True)
    sent = []

    async def send(message):
        sent.append(message)

    bridge.send = send
    asyncio.run(bridge.answer_dynamic_tool(60, {"status": "installed"}))
    assert sent == [{"id": 60, "result": {"contentItems": [{"type": "inputText",
        "text": "依存関係を導入しました。ビルドとテストを続けてください。"}], "success": True}}]


def test_gemini_receives_the_same_no_argument_dependency_tool(tmp_path):
    called = 0

    async def installer():
        nonlocal called
        called += 1
        return {"status": "installed"}

    tool = sdk_tools(tmp_path, Progress(), installer)[-1]
    assert tool.__name__ == "app_forge_install_dependencies"
    assert not inspect.signature(tool).parameters
    assert asyncio.run(tool()) == {"status": "installed"}
    assert called == 1
