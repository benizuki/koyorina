"""Google Antigravity managed-agent adapter.

Antigravity executes in Google's remote sandbox.  This adapter treats that sandbox
as an untrusted build worker: sources are uploaded explicitly, the resulting
snapshot is downloaded into a temporary directory, and Koyorina's normal bundle
validation remains authoritative.
"""
import asyncio
import io
import json
import os
import tarfile
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

import httpx

from backend.domain.generation import artifact_path_is_allowed, is_generated_leftover


API_ROOT = "https://generativelanguage.googleapis.com/v1beta"
# Managed Agents/Antigravity currently require this preview revision header.  Without
# it the endpoint responds with a misleading 404 even though the API key is valid.
API_REVISION = "2026-05-20"
TERMINAL = {"completed", "failed", "cancelled", "incomplete", "expired"}


def _raise_for_status(response, operation):
    """Raise a diagnosable error without leaking request headers or payloads."""
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        detail = response.text.replace("\n", " ").strip()[:500]
        raise RuntimeError(
            f"Antigravity {operation} failed: HTTP {response.status_code}: {detail}"
        ) from exc


def _text(value):
    return value if isinstance(value, str) else str(value or "")


def _step_label(step_type):
    labels = {
        "thought": "作業方針の整理",
        "model_output": "生成結果の作成",
        "function_call": "ツールの実行",
        "code_execution_call": "コードの実行",
        "code_execution_result": "コード実行結果の確認",
        "file_search_call": "ファイルの検索",
    }
    return labels.get(step_type, f"Remote Sandboxの{step_type or '作業'}")


async def _poll_interaction(http, interaction_id, interaction, headers, progress):
    """SSE切断後も同じInteractionを追跡する。"""
    while True:
        status = _text(getattr(interaction, "status", ""))
        progress.record("activity", f"AntigravityのRemote Sandboxを実行しています（{status}）。")
        if status in TERMINAL:
            return interaction
        await asyncio.sleep(5)
        response = await http.get(f"{API_ROOT}/interactions/{interaction_id}")
        if response.status_code in {408, 429, 500, 502, 503, 504}:
            progress.record(
                "activity",
                f"Antigravityの状態確認が一時的に失敗しました（HTTP {response.status_code}）。再確認します。",
            )
            continue
        _raise_for_status(response, "interaction poll")
        interaction = SimpleNamespace(**response.json())


def _usage(interaction):
    usage = getattr(interaction, "usage", None) or {}
    if not isinstance(usage, dict):
        usage = getattr(usage, "model_dump", lambda: {})()
    return {key: int(usage[key]) for key in
            ("input_tokens", "output_tokens", "total_tokens")
            if str(usage.get(key, "")).isdigit()}


def _completion_detail(interaction):
    """Return a short, non-sensitive explanation supplied by the API.

    Antigravity uses ``incomplete`` for a completed interaction whose result was
    cut short (most commonly the token budget).  Keep the detail for operators,
    but never include prompts, response text, or request headers in progress
    messages.
    """
    for name in ("incomplete_details", "status_details", "error"):
        value = getattr(interaction, name, None)
        if value:
            if isinstance(value, dict):
                safe = {key: value[key] for key in ("reason", "code", "message")
                        if key in value}
                if safe:
                    return json.dumps(safe, ensure_ascii=False, separators=(",", ":"))[:300]
            return str(value).replace("\n", " ")[:300]
    return ""


def _sources(workspace: Path):
    result = []
    for path in workspace.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(workspace).as_posix()
        if is_generated_leftover(PurePosixPath(relative).parts):
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if len(content.encode()) > 200_000:
            continue
        target = ".agents/AGENTS.md" if relative == "AGENTS.md" else relative
        result.append({"type": "inline", "target": target, "content": content})
    return result[:100]


def _snapshot_files(data: bytes):
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as archive:
        files = []
        for member in archive.getmembers():
            if not member.isfile() or member.issym() or member.islnk():
                continue
            parts = tuple(part for part in PurePosixPath(member.name).parts if part not in {"", "."})
            if not parts or ".." in parts:
                continue
            files.append((parts, archive.extractfile(member).read()))
    prefixes = [parts[:i] for parts, _ in files for i in range(len(parts))]
    root = next((prefix for prefix in sorted(set(prefixes), key=len)
                 if any(parts[len(prefix):] == ("backend", "main.py") for parts, _ in files)), ())
    if not root and files:
        # A token-limited interaction can finish before creating backend/main.py.
        # The files endpoint still returns a snapshot, commonly below one
        # generated directory (for example ``workspace/frontend/...``). Strip
        # that transport-only directory so partial files are merged into the
        # real workspace instead of being written below ``workspace/``.
        first_parts = {parts[0] for parts, _ in files if parts}
        known = {"backend", "frontend", "pyproject.toml", "AGENTS.md",
                 "README.md", ".env.example", ".gitignore"}
        if len(first_parts) == 1 and next(iter(first_parts)) not in known:
            root = (next(iter(first_parts)),)
    result = []
    for parts, content in files:
        relative_parts = parts[len(root):] if root and parts[:len(root)] == root else parts
        relative = "/".join(relative_parts)
        if not relative or not artifact_path_is_allowed(relative) or len(content) > 200_000:
            continue
        try:
            result.append((relative, content.decode("utf-8")))
        except UnicodeDecodeError:
            continue
    return result


async def run(workspace: Path, *, prompt: str, model: str, agent: str,
              api_key: str, max_total_tokens: int, progress):
    if not api_key:
        raise RuntimeError("Antigravity用のGemini APIキーが設定されていません。")
    headers = {"x-goog-api-key": api_key, "Api-Revision": API_REVISION,
               "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=None, headers=headers) as http:
        # SSEを使うとRemote Sandboxのステップをリアルタイムに受け取れる。
        # ストリームが途中で切れても、取得済みのInteraction IDからポーリングへ戻す。
        sources = _sources(workspace)
        progress.record("status", f"既存ファイル{len(sources)}件をRemote Sandboxへ渡しています。")
        request = {
            "agent": agent,
            "input": prompt,
            "environment": {"type": "remote", "sources": sources},
            "agent_config": {"type": "antigravity", "model": model,
                              "max_total_tokens": max_total_tokens},
            "background": True,
            "stream": True,
        }
        interaction = None
        interaction_id = ""
        environment_id = ""
        step_types = {}
        try:
            async with http.stream("POST", f"{API_ROOT}/interactions", json=request) as response:
                _raise_for_status(response, "interaction create")
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    raw = line[5:].strip()
                    if not raw or raw == "[DONE]":
                        continue
                    try:
                        event = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    event_type = event.get("event_type", "")
                    item = event.get("interaction") or {}
                    if event_type == "interaction.created":
                        interaction_id = item.get("id", "")
                        environment_id = item.get("environment_id", "")
                        interaction = SimpleNamespace(**item)
                        progress.record("activity", "AntigravityのRemote Sandboxを開始しました。")
                    elif event_type == "interaction.status_update":
                        progress.record("activity", f"Remote Sandboxの状態: {event.get('status', '更新')}")
                    elif event_type == "step.start":
                        step = event.get("step") or {}
                        index = event.get("index")
                        step_type = str(step.get("type") or "unknown")
                        if isinstance(index, int):
                            step_types[index] = step_type
                            suffix = f"（ステップ {index + 1}）"
                        else:
                            suffix = ""
                        progress.record("activity", f"{_step_label(step_type)}を開始{suffix}")
                    elif event_type == "step.stop":
                        index = event.get("index")
                        step_type = step_types.get(index, "unknown")
                        suffix = f"（ステップ {index + 1}）" if isinstance(index, int) else ""
                        progress.record("activity", f"{_step_label(step_type)}が完了{suffix}")
                    elif event_type == "interaction.completed":
                        interaction = SimpleNamespace(**item)
                        interaction_id = getattr(interaction, "id", "") or interaction_id
                        environment_id = getattr(interaction, "environment_id", "") or environment_id
                        break
        except (httpx.ReadError, httpx.RemoteProtocolError, httpx.TimeoutException):
            if not interaction_id:
                raise
            progress.record("activity", "進捗ストリームが切断されたため、状態確認へ切り替えます。")
        if not interaction_id:
            raise RuntimeError("AntigravityのInteraction IDを取得できませんでした。")
        if interaction is None:
            interaction = SimpleNamespace(id=interaction_id, status="in_progress",
                                          environment_id=environment_id)
        if _text(getattr(interaction, "status", "")) not in TERMINAL:
            interaction = await _poll_interaction(http, interaction_id, interaction, headers, progress)
        status = _text(getattr(interaction, "status", ""))
    partial = status == "incomplete"
    if status not in {"completed", "incomplete"}:
        detail = _completion_detail(interaction)
        suffix = f" 詳細: {detail}" if detail else ""
        raise RuntimeError(f"Antigravity interactionが{status}で終了しました。{suffix}")
    if partial:
        detail = _completion_detail(interaction)
        suffix = f"（{detail}）" if detail else ""
        progress.record(
            "status",
            "Antigravityの生成は途中終了しました。取得済みの成果物を検査・修正します" + suffix,
        )
    if not environment_id:
        environment_id = getattr(interaction, "environment_id", "")
    async with httpx.AsyncClient(timeout=120, headers=headers) as http:
        # The legacy files/environment-*:download endpoint is deprecated and
        # returns an HTTP error for current managed-agent environments.
        response = await http.get(f"{API_ROOT}/environments/{environment_id}/files",
                                  params={"alt": "media", "recursive": "true"})
        _raise_for_status(response, "artifact download")
    files = _snapshot_files(response.content)
    if not files:
        if partial:
            detail = _completion_detail(interaction)
            suffix = f" 詳細: {detail}" if detail else ""
            raise RuntimeError(
                "Antigravityが途中終了し、取得できる成果物がありません。" + suffix
            )
        raise RuntimeError("Antigravityの成果物スナップショットが空です。")
    progress.record("status", f"Remote Sandboxの成果物{len(files)}件を既存workspaceへマージしています。")
    for relative, content in files:
        target = workspace / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return _usage(interaction)
