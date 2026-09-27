"""OpenAI SDK compatible generation provider.

The endpoint is configured by App Forge and is never supplied by a user.  The
model receives only the same workspace tools as the Gemini provider.
"""
import asyncio
import base64
import json
import os
import time
from pathlib import Path
from contextlib import suppress
from urllib.parse import urlparse

from backend.domain import attachments
from backend.domain.generation import conventions, generation_prompt, instruction_prompt
from backend.domain.generation_progress import safe_report
from backend.domain.workspace_tools import check_sources, list_sources, read_source, write_source

TURN_TIMEOUT = 1800
MAX_TOOL_ROUNDS = 80


class ProviderUnavailable(RuntimeError):
    pass


def usage_of(response) -> dict:
    usage = getattr(response, "usage", None)
    if usage is None:
        return {}
    result = {}
    for target, source in (("input_tokens", "prompt_tokens"),
                           ("output_tokens", "completion_tokens"),
                           ("total_tokens", "total_tokens")):
        value = getattr(usage, source, None)
        if isinstance(value, int) and value >= 0:
            result[target] = value
    return result


def _tool_specs() -> list[dict]:
    return [
        {"type": "function", "function": {"name": "write_file",
         "description": "Create or replace one source file in the project.",
         "parameters": {"type": "object", "properties": {"path": {"type": "string"},
                         "text": {"type": "string"}}, "required": ["path", "text"],
                         "additionalProperties": False}}},
        {"type": "function", "function": {"name": "read_file",
         "description": "Read one existing source file of the project.",
         "parameters": {"type": "object", "properties": {"path": {"type": "string"}},
                         "required": ["path"], "additionalProperties": False}}},
        {"type": "function", "function": {"name": "list_files",
         "description": "List source files that currently exist in the project.",
         "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}},
        {"type": "function", "function": {"name": "check_files",
         "description": "Check required files and Python/JSON syntax.",
         "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}},
        {"type": "function", "function": {"name": "app_forge_install_dependencies",
         "description": "Install dependencies declared in this workspace. This function takes no arguments.",
         "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}},
    ]


def _tools(workspace: Path, progress, installer=None):
    def write_file(path: str, text: str):
        result = write_source(workspace, path, text)
        if result.get("status") == "written":
            # path は書き込めた時点で artifact_path_is_allowed を通っている（英数字と ._/- だけ）。
            # 伏せ字にかけると、長い部品名が「識別子非表示」に化ける。
            progress.record("file", f"ファイルを更新しました: {path}。",
                            response=True, state="done")
        return result

    async def install():
        return await installer() if installer else {"status": "unavailable", "problems": ["依存導入は未設定です。"]}

    return {"write_file": write_file, "read_file": lambda path: read_source(workspace, path),
            "list_files": lambda: list_sources(workspace),
            "check_files": lambda: check_sources(workspace),
            "app_forge_install_dependencies": install}


def _content(response) -> str:
    return (getattr(response, "content", None) or "").strip()


async def run(workspace: Path, specification, instruction, progress, *, model: str,
              base_url: str, api_key: str, installer=None, notes="", prompt_override=None) -> dict:
    if not base_url or not api_key or not model:
        raise ProviderUnavailable("OpenAI互換APIの接続設定が不足しています。")
    try:
        from openai import AsyncOpenAI
    except ImportError as exc:
        raise ProviderUnavailable("OpenAI SDKが生成Podにインストールされていません。") from exc

    headers = {}
    if urlparse(base_url).hostname == "openrouter.ai":
        # OpenRouterのランキング用メタデータ。認証情報やプロンプトは入れない。
        headers = {"HTTP-Referer": os.getenv("OPENAI_COMPATIBLE_REFERER", "http://app-forge.local"),
                   "X-OpenRouter-Title": os.getenv("OPENAI_COMPATIBLE_TITLE", "App Forge")}
    client = AsyncOpenAI(api_key=api_key, base_url=base_url.rstrip("/"),
                         default_headers=headers or None, timeout=120.0, max_retries=0)
    tool_map = _tools(workspace, progress, installer)
    text = (prompt_override if prompt_override is not None else
            (instruction_prompt(instruction) if instruction else generation_prompt(specification))) + notes
    messages = [{"role": "system", "content": conventions(with_skills=False) +
                 "\n\nUse the tools to create and edit files. You cannot run commands. "
                 "Call check_files before finishing and fix every problem."},
                {"role": "user", "content": text}]
    pictures = attachments.images(workspace)[:attachments.MAX_ATTACHMENTS]
    documents = [path for path in attachments.documents(workspace)[:attachments.MAX_ATTACHMENTS]
                 if path not in pictures]
    for path in documents:
        messages[1]["content"] += f"\n添付資料: {path.name}"
    if pictures:
        # OpenAI互換APIで共通の画像入力形式。非対応モデルは提供元側で明示的に
        # 400を返すため、生成経路のログに原因を残し、勝手な別APIへ切り替えない。
        content = [{"type": "text", "text": text}]
        for path in pictures:
            import mimetypes
            mime = mimetypes.guess_type(path.name)[0] or "image/png"
            content.append({"type": "image_url", "image_url": {
                "url": f"data:{mime};base64:{base64.b64encode(path.read_bytes()).decode('ascii')}"}})
        messages[1]["content"] = content
    usage = {}
    progress.record("status", f"生成を開始しました（{model}／OpenAI互換API）。応答を待っています。")
    try:
        async with asyncio.timeout(TURN_TIMEOUT):
            for _ in range(MAX_TOOL_ROUNDS):
                response = await client.chat.completions.create(
                    model=model, messages=messages, tools=_tool_specs(), tool_choice="auto")
                current = usage_of(response)
                for key, value in current.items():
                    usage[key] = usage.get(key, 0) + value
                choice = response.choices[0]
                message = choice.message
                if message.content:
                    progress.record("codex", safe_report(message.content), response=True,
                                    key="openai-compatible-response")
                calls = message.tool_calls or []
                serialized = (message.model_dump(exclude_none=True)
                              if hasattr(message, "model_dump") else
                              {"role": "assistant", "content": message.content,
                               "tool_calls": [{"id": call.id, "type": "function",
                                                "function": {"name": call.function.name,
                                                              "arguments": call.function.arguments}}
                                               for call in calls]})
                messages.append(serialized)
                if not calls:
                    return usage
                for call in calls:
                    name = call.function.name
                    try:
                        args = json.loads(call.function.arguments or "{}")
                        fn = tool_map.get(name)
                        if fn is None:
                            result = {"status": "rejected", "reason": "未対応のツールです。"}
                        else:
                            result = fn(**args) if name != "app_forge_install_dependencies" else await fn()
                            if asyncio.iscoroutine(result):
                                result = await result
                    except Exception as exc:
                        result = {"status": "error", "reason": str(exc)[:300]}
                    messages.append({"role": "tool", "tool_call_id": call.id,
                                    "content": json.dumps(result, ensure_ascii=False)[:20000]})
                progress.record("activity", "OpenAI互換APIのツール処理を続けています。",
                                key="openai-compatible-tools", state="running")
        raise ProviderUnavailable("OpenAI互換APIのツール処理が上限に達しました。")
    finally:
        await client.close()
