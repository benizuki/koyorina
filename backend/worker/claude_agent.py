"""Claude でのコード生成。Claude Agent SDK を生成エージェントの Pod の中で動かす。

Gemini（antigravity_sdk_agent）と同じ考え方にする。
- 組み込みの道具（Bash・Read・Write・Edit・Web など）はすべて止める（tools=[]）。
- ファイルの読み書きは Koyorina の道具（workspace_tools。作業場所の外へは届かない）だけを、
  SDK の自前 MCP サーバーとして渡す。コマンドは実行させない。
- 設定ファイル（~/.claude など）は読まない。利用状況・エラー報告の送信は止める。
- 確かめるのは常に基盤（verify_and_repair）で、モデルの自己申告は通らない。

接続先はシステム設定（またはテナントの設定）の Claude に従う。
Claude on Vertex AI（認証は Gemini と同じ鍵ファイルか WIF）か、Anthropic の APIキー。
Claude のサブスクリプション（claude.ai のログイン）は使わない。第三者の製品で使わせることは認められていない。
"""
from __future__ import annotations

import asyncio
import inspect
import json
import os
import shutil
import tempfile
from pathlib import Path

from backend.domain.generation import conventions, generation_prompt, instruction_prompt
from backend.domain.generation_progress import safe_report
from backend.worker.gemini_agent import ProviderUnavailable, QuotaExceeded, sdk_tools

TURN_TIMEOUT = 1800
MAX_TURNS = 120
SAY_KEY = "claude-say"
SERVER = "koyorina"
SYSTEM_NOTE = ("\n\nUse the koyorina tools to create and edit files. "
               "You cannot run commands. Call check_files before finishing and fix every problem.")
EFFORTS = {"low", "medium", "high", "xhigh", "max"}


def endpoint_env(config_dir: str) -> dict[str, str]:
    """SDK（その中の Claude Code）へ渡す環境変数。接続先と、外へ送らない設定。"""
    env = {
        # 設定と会話の記録は、このターンだけの置き場へ。永続する保存領域には残さない。
        "CLAUDE_CONFIG_DIR": config_dir,
        # 利用状況・エラー報告・更新確認などの送信を止める。業務データを扱うので必ず切る。
        "DISABLE_TELEMETRY": "1", "DISABLE_ERROR_REPORTING": "1",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "DISABLE_AUTOUPDATER": "1",
    }
    if os.getenv("AGENT_CLAUDE_BACKEND", "api_key") == "vertex":
        credentials = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "")
        project = os.getenv("AGENT_CLAUDE_PROJECT", "")
        if not (project and credentials and Path(credentials).is_file()):
            raise RuntimeError("Claude on Vertex AI の認証がありません。システム設定の Gemini で Vertex AI"
                               "（Workload Identity 連携か鍵ファイル）を設定するか、Claude を APIキーにしてください。")
        return {**env, "CLAUDE_CODE_USE_VERTEX": "1", "ANTHROPIC_VERTEX_PROJECT_ID": project,
                "CLOUD_ML_REGION": os.getenv("AGENT_CLAUDE_LOCATION", "global")}
    key = os.getenv("AGENT_CLAUDE_API_KEY", "")
    if not key:
        raise RuntimeError("Claude の APIキーが設定されていません。システム設定の Claude で入れてください。")
    return {**env, "ANTHROPIC_API_KEY": key}


def mcp_tools(callables: list) -> list:
    """Koyorina の道具を、SDK の自前 MCP サーバーの道具にする。引数はすべて文字列。"""
    from claude_agent_sdk import tool
    wrapped = []
    for fn in callables:
        params = {name: str for name in inspect.signature(fn).parameters}

        async def handler(args, _fn=fn):
            value = _fn(**{name: args.get(name, "") for name in inspect.signature(_fn).parameters})
            if inspect.isawaitable(value):
                value = await value
            return {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}]}
        wrapped.append(tool(fn.__name__, (fn.__doc__ or fn.__name__).strip(), params)(handler))
    return wrapped


def build_options(workspace: Path, model: str, tools: list, config_dir: str, effort: str = ""):
    from claude_agent_sdk import ClaudeAgentOptions, create_sdk_mcp_server
    return ClaudeAgentOptions(
        # 組み込みの道具はすべて止め、Koyorina の道具だけを許す。
        tools=[], allowed_tools=[f"mcp__{SERVER}__{fn.__name__}" for fn in tools],
        mcp_servers={SERVER: create_sdk_mcp_server(SERVER, tools=mcp_tools(tools))}, strict_mcp_config=True,
        # スキルは載せない。道具が作業場所の中しか見えないので、作業場所の外のスキルは読めない。
        system_prompt=conventions(with_skills=False) + SYSTEM_NOTE,
        setting_sources=[], cwd=str(workspace), model=model or None,
        effort=effort if effort in EFFORTS else None, max_turns=MAX_TURNS,
        env=endpoint_env(config_dir))


def classify(status, text: str) -> BaseException:
    if status == 429 or "rate_limit" in text or "429" in text:
        return QuotaExceeded("Claude API rate limited")
    if status in {500, 502, 503, 529} or "overloaded" in text:
        return ProviderUnavailable("Claude API temporarily unavailable")
    return RuntimeError(f"Claude generation failed: {text[:300]}")


async def run(workspace: Path, *, prompt: str, model: str, progress, effort: str = "",
              installer=None, query=None, options_builder=None) -> dict:
    """1ターン。書いたファイルは workspace に残る。使ったトークンを返す。"""
    tools = sdk_tools(workspace, progress, installer)
    config_dir = tempfile.mkdtemp(prefix="claude-sdk-", dir="/tmp" if Path("/tmp").is_dir() else None)
    detail = "／".join(filter(None, [model, effort]))
    progress.record("status", f"生成を開始しました（{detail}／Claude）。応答を待っています。")
    try:
        options = (options_builder or build_options)(workspace, model, tools, config_dir, effort)
        if query is None:
            from claude_agent_sdk import query
        pieces, usage = [], {}
        async with asyncio.timeout(TURN_TIMEOUT):
            async for message in query(prompt=prompt, options=options):
                kind = type(message).__name__
                if kind == "AssistantMessage":
                    for block in message.content:
                        if type(block).__name__ == "TextBlock" and block.text:
                            pieces.append(block.text)
                            # 最後の報告はこの行になる（Gemini と同じく1行を書き換えていく）。
                            if text := safe_report("\n\n".join(pieces)):
                                progress.record("codex", text, response=True, key=SAY_KEY)
                elif kind == "ResultMessage":
                    usage = usage_of(message.usage)
                    if message.is_error:
                        raise classify(message.api_error_status, " ".join(map(str, message.errors or []))
                                       + " " + str(message.result or ""))
        return usage
    finally:
        shutil.rmtree(config_dir, ignore_errors=True)


async def claude_turn(workspace: Path, model: str, specification, instruction, progress,
                      effort="", notes="", text=None, installer=None, **sdk) -> dict:
    """生成エージェントから呼ぶ入口。text を渡すと、その依頼文でそのまま回す（検査の失敗を戻す修正）。"""
    prompt = text if text is not None else (
        (instruction_prompt(instruction) if instruction else generation_prompt(specification)) + notes)
    return await run(workspace, prompt=prompt, model=model, progress=progress, effort=effort,
                     installer=installer, **sdk)


def usage_of(usage: dict | None) -> dict:
    """Claude の使用量を、Koyorina の数え方（入力・出力・キャッシュ・合計）にする。"""
    usage = usage or {}
    read = usage.get("cache_read_input_tokens") or 0
    created = usage.get("cache_creation_input_tokens") or 0
    base = usage.get("input_tokens") or 0
    output = usage.get("output_tokens") or 0
    result = {"input_tokens": base + read + created, "output_tokens": output, "cached_tokens": read,
              "total_tokens": base + read + created + output}
    return result if any(result.values()) else {}
