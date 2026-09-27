"""Gemini でのコード生成。中身は google-antigravity SDK（手元で動くエージェント）。

SDK の実行部（localharness）は生成エージェントの Pod の中で動く。使うのはコードを書いて
直すところだけで、要件のヒアリングは google-genai のまま（gemini_agent）。

SDK のコマンド実行の隔離（exebox）は、Pod 相当のコンテナで「効いている」と報告しながら
作業場所の外へ書き込めてしまった（2026-09 の試験）。そのため SDK の組み込みの道具は
FINISH 以外すべて止め、ファイルの読み書きは Koyorina の道具（workspace_tools。作業場所の
外へは届かない）だけを渡す。コマンドは実行させない。確かめるのは常に基盤
（verify_and_repair）で、モデルの自己申告は通らない。

接続先はシステム設定の Gemini に従う。Vertex AI は鍵ファイルか Workload Identity 連携
（どちらも GOOGLE_APPLICATION_CREDENTIALS）、Gemini API は APIキー。
"""
from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from pathlib import Path

from backend.domain import attachments
from backend.domain.generation import conventions, generation_prompt, instruction_prompt
from backend.domain.generation_progress import safe_report
from backend.worker.gemini_agent import ProviderUnavailable, QuotaExceeded, sdk_tools

TURN_TIMEOUT = 1800
SAY_KEY = "gemini-say"
SYSTEM_NOTE = ("\n\nUse the tools to create and edit files. "
               "You cannot run commands. Call check_files before finishing and fix every problem.")


def endpoint() -> dict:
    """接続先。GEMINI_API_BACKEND（システム設定の Gemini）に従う。"""
    if os.getenv("GEMINI_API_BACKEND", "vertex") == "developer":
        key = os.getenv("GEMINI_API_KEY", "")
        if not key:
            raise RuntimeError("Gemini API のAPIキーが設定されていません。"
                               "システム設定の Gemini でAPIキーを入れてください。")
        return {"kind": "developer", "api_key": key}
    credentials = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "")
    if not (os.getenv("GOOGLE_CLOUD_PROJECT") and credentials and Path(credentials).is_file()):
        raise RuntimeError("Vertex AI の認証がありません。システム設定の Gemini で Workload Identity 連携を"
                           "設定するか、鍵ファイルを配備してください（トークンファイル方式は使えません）。")
    return {"kind": "vertex", "project": os.environ["GOOGLE_CLOUD_PROJECT"],
            "location": os.getenv("GOOGLE_CLOUD_LOCATION", "global")}


def classify(exc: BaseException) -> BaseException:
    """SDK の例外を、Koyorina の失敗の種類（枠切れ・混雑）へ読み替える。"""
    text = f"{type(exc).__name__} {exc}"
    if "RESOURCE_EXHAUSTED" in text or "Error 429" in text:
        return QuotaExceeded("Gemini API quota exhausted")
    if "UNAVAILABLE" in text or "Error 503" in text:
        return ProviderUnavailable("Gemini API temporarily unavailable")
    return exc


def build_config(workspace: Path, model: str, tools: list, state_dir: str, thinking_level: str = ""):
    from google.antigravity import BuiltinTools, CapabilitiesConfig, LocalAgentConfig, ModelTarget
    from google.antigravity.connections.connection import DebugConfig
    from google.antigravity.hooks import policy
    from google.antigravity.models import (GeminiAPIEndpoint, GeminiModelOptions, ModelType,
                                           ThinkingLevel, VertexEndpoint)
    from google.antigravity.types import ModelAPIRetryConfig, RetryConfig
    target = endpoint()
    options = GeminiModelOptions(thinking_level=ThinkingLevel(thinking_level.lower())) if thinking_level else None
    where = (GeminiAPIEndpoint(api_key=target["api_key"], options=options) if target["kind"] == "developer"
             else VertexEndpoint(project=target["project"], location=target["location"], options=options))
    return LocalAgentConfig(
        model=ModelTarget(name=model, types=[ModelType.TEXT], endpoint=where),
        workspaces=[str(workspace)], tools=tools,
        # スキルの一覧は載せない。コマンドを実行できず、道具も作業場所の中しか見えないので、
        # スキル（作業場所の外）は読めない。載せると「あると書いてあるのに読めない」ことになる。
        system_instructions=conventions(with_skills=False) + SYSTEM_NOTE,
        # 組み込みの道具は終了だけ。読み書きは Koyorina の道具（作業場所の中だけ）に限る。
        capabilities=CapabilitiesConfig(enable_subagents=False, enabled_tools=[BuiltinTools.FINISH]),
        policies=[policy.deny_all(), policy.allow(BuiltinTools.FINISH.value),
                  *[policy.allow(tool.__name__) for tool in tools]],
        retry_config=RetryConfig(api_retry=ModelAPIRetryConfig(
            max_retries=5, initial_sleep_duration_ms=10000, exponential_multiplier=2.0)),
        # 既定では動作の追跡情報を Google へ送る。業務データを扱うので必ず切る。
        debug_config=DebugConfig(enable_server_side_tracing=False, logging_level="WARNING"),
        # 会話の記録は /tmp（Pod ごとに消える）へ。永続する保存領域には残さない。
        app_data_dir=state_dir, save_dir=state_dir)


def attached(workspace: Path) -> list:
    """依頼に添えた画像・PDF。テキストの添付は依頼文に入っている（attachments.prompt_section）。"""
    from google.antigravity import from_bytes
    return [from_bytes(path.read_bytes(), attachments.media_type(path))
            for path in attachments.documents(workspace)[:attachments.MAX_ATTACHMENTS]]


async def run(workspace: Path, *, prompt, model: str, progress, thinking_level: str = "",
              installer=None, agent_factory=None, config_builder=None) -> dict:
    """1ターン。書いたファイルは workspace に残る。使ったトークンを返す。"""
    tools = sdk_tools(workspace, progress, installer)
    state_dir = tempfile.mkdtemp(prefix="gemini-sdk-", dir="/tmp" if Path("/tmp").is_dir() else None)
    detail = "／".join(filter(None, [model, thinking_level]))
    progress.record("status", f"生成を開始しました（{detail}）。Geminiの応答を待っています。")
    try:
        config = (config_builder or build_config)(workspace, model, tools, state_dir, thinking_level)
        if agent_factory is None:
            from google.antigravity import Agent
            agent_factory = Agent
        async with asyncio.timeout(TURN_TIMEOUT):
            async with agent_factory(config) as agent:
                response = await agent.chat(prompt)
                pieces = []
                async for chunk in response.chunks:
                    if isinstance(chunk, str) or type(chunk).__name__ == "Text":
                        pieces.append(getattr(chunk, "text", chunk))
                        # 届いたぶんを1行として書き換えていく。最後の報告はこの行になる。
                        if text := safe_report("".join(pieces)):
                            progress.record("codex", text, response=True, key=SAY_KEY)
                return usage_of(getattr(response, "usage_metadata", None))
    except (QuotaExceeded, ProviderUnavailable, TimeoutError):
        raise
    except Exception as exc:
        mapped = classify(exc)
        if mapped is exc:
            raise
        raise mapped from exc
    finally:
        shutil.rmtree(state_dir, ignore_errors=True)


async def gemini_turn(workspace: Path, model: str, specification, instruction, progress,
                      thinking_level="", notes="", text=None, installer=None, **sdk) -> dict:
    """生成エージェントから呼ぶ入口。text を渡すと、その依頼文でそのまま回す（検査の失敗を戻す修正）。"""
    prompt = text if text is not None else (
        (instruction_prompt(instruction) if instruction else generation_prompt(specification)) + notes)
    files = attached(workspace) if attachments.documents(workspace) else []
    return await run(workspace, prompt=[prompt, *files] if files else prompt, model=model,
                     progress=progress, thinking_level=thinking_level, installer=installer, **sdk)


def usage_of(metadata) -> dict:
    names = {"input_tokens": "prompt_token_count", "output_tokens": "candidates_token_count",
             "cached_tokens": "cached_content_token_count", "total_tokens": "total_token_count"}
    return {target: value for target, source in names.items()
            if isinstance(value := getattr(metadata, source, None), int) and value >= 0}
