"""Codex App Server stdio transport. Never expose arbitrary RPC to HTTP clients."""
import asyncio
from collections import deque
from contextlib import suppress
import json
import logging
import os
import re
import time
from pathlib import Path
from uuid import UUID
from backend.domain.generation import generation_permission_args, install_generation_skills
from backend.domain.interview import MAX_OPTIONS, MAX_QUESTIONS

logger = logging.getLogger("uvicorn.error")
# 資格情報らしい連なりは診断ログへ出さない。原因調査に要るのは前後の文で、値ではない。
SECRET_LIKE = re.compile(r"[A-Za-z0-9._\-]{24,}")
STDERR_LINES = 20
STDERR_LINE_CHARS = 500


class CodexTransportClosed(RuntimeError):
    """接続そのものが落ちた。要求が拒否された場合と区別する。"""


class CodexTurnFailed(RuntimeError):
    """ターンそのものが失敗した。接続も要求も通っている。

    args[0] にCodexが返した手掛かり（伏字済み）を入れる。やり直す価値があるのか、
    待つしかないのかは、その中身でしか分からない。
    """


# 枠切れは待つ以外にできることがない。やり直しを促すと、無駄に枠を消費させるうえ、
# 直らない操作を何度も試させることになる。ここで見分ける。
USAGE_LIMIT = "usageLimitExceeded"
RESUMES_AT = re.compile(r"try again at ([^\\\"'.]{4,60})")


def usage_limited(detail) -> bool:
    return USAGE_LIMIT in str(detail or "")


def usages_resume_at(detail) -> str:
    """回復する時刻。Codexの文面から拾えたときだけ返す。"""
    match = RESUMES_AT.search(str(detail or ""))
    return match[1].strip() if match else ""


def redacted(text: str) -> str:
    return SECRET_LIKE.sub("[redacted]", text)[:STDERR_LINE_CHARS]


def workspaces_root(root: Path) -> Path:
    """生成コードの置き場。利用者ではなくアプリで分かれる。

    同じアプリを誰が触っても、続きから進められるようにするため。認証は利用者ごとの
    home に残し、ここにはコードとジョブだけを置く。
    """
    return root.resolve() / "projects"


def isolated_environment(root: Path, user_id: str, skills_root: Path | None = None):
    # 実行ユーザーに紐づくUUID以外をパスへ受け入れない。
    identity = str(UUID(user_id))
    home = root.resolve() / identity
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    home.chmod(0o700)
    # APIキー・既存Codexセッション・クラウド認証を子プロセスへ継承しない。
    env = {key: os.environ[key] for key in ("PATH", "SYSTEMROOT", "LANG", "TMPDIR") if key in os.environ}
    env.update({"HOME": str(home), "CODEX_HOME": str(home / "codex")})
    codex_home = home / "codex"
    codex_home.mkdir(exist_ok=True, mode=0o700)
    # Codexがスキルを見つけるのはこの場所。ここから出すと、読めるようにはなっても
    # モデルへ提示されなくなる。提示用はここに置き、サンドボックスへは許可を出さない
    # （同じ場所に資格情報がある）。資産を読ませるための複製は作業場所の隣に置く。
    install_generation_skills(codex_home / "skills", skills_root)
    return home, env


def skills_directory(working_directory: Path) -> Path:
    """スキルの置き場。作業場所の中の `.agents/skills`。

    ここに置くと、用途が1か所で足りる。
      - Codexがプロジェクト単位のスキルを見つける場所の慣習に合う
      - 作業場所の中なので、サンドボックスからそのまま読める
        （資産のコピーを指示するスキルがあり、読めないと手順が成立しない）

    成果物と変更履歴からは名前で除外する（`.agents` は IGNORED_GENERATED_PARTS）。
    CODEX_HOMEにも配るが、あちらは提示用。読み取り許可は出さない
    （同じ場所に資格情報がある）。
    """
    return working_directory / ".agents" / "skills"


class CodexBridge:
    """stdio接続。トークンや任意RPCをHTTPへ公開しない。"""
    def __init__(self, binary: str, root: Path, user_id: str, *, allow_login: bool = False,
                 allow_generation: bool = False, working_directory: Path | None = None,
                 skills_root: Path | None = None, allow_user_input: bool = False):
        self.binary, self.root, self.user_id = binary, root, user_id
        self.allow_login = allow_login
        self.allow_generation = allow_generation
        self.working_directory = working_directory
        self.skills_root = skills_root
        self.allow_user_input = allow_user_input
        self.sequence = 0
        self.notifications = asyncio.Queue(maxsize=256)
        self.pending = {}
        self.reader = None
        self.write_lock = asyncio.Lock()
        self.process = None
        self.last_activity = 0.0
        self.response_bytes = {}
        # 異常終了の手掛かり。直近の数行だけを持ち、落ちたときにまとめて出す。
        self.stderr_tail = deque(maxlen=STDERR_LINES)
        self.stderr_task = None
        self.closing = False

    async def __aenter__(self):
        home, env = isolated_environment(self.root, self.user_id, self.skills_root)
        working_directory = home
        if self.working_directory is not None:
            # 作業場所はアプリ単位の共有領域にある。homeの下ではないので、
            # 「共有領域の下か」で確かめる。ここを緩めると、任意のパスをcwdにできる。
            working_directory = self.working_directory.resolve(strict=True)
            working_directory.relative_to(workspaces_root(self.root))
            # 提示用（CODEX_HOME）とは別に、読ませる用をアプリ単位で置く。
            # 資産のコピーを指示するスキルがあり、CODEX_HOME はサンドボックスの
            # 外なので、そちらだけでは cp が Permission denied になる。
            skills = skills_directory(working_directory)
            skills.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            install_generation_skills(skills, self.skills_root)
            env["APP_FORGE_SKILLS"] = str(skills)
            # 作業場所の中なので、読むための追加の許可は要らない。
        # 履歴は読み取りだけ許す。モデルが差分を見て直せるようにしつつ、
        # `git reset` や削除で履歴を壊せないようにする。
        permission_args = (generation_permission_args(
                               str(working_directory), str(self.root.resolve() / "history"))
                           if self.working_directory is not None
                           else ["-c", 'default_permissions=":read-only"'])
        self.process = await asyncio.create_subprocess_exec(
            self.binary, "app-server", "--listen", "stdio://", "-c", 'cli_auth_credentials_store="file"',
            *permission_args,
            cwd=working_directory, env=env, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, limit=1024 * 1024)
        self.reader = asyncio.create_task(self._read())
        self.stderr_task = asyncio.create_task(self._drain_stderr())
        try:
            await self.call("initialize", {"clientInfo": {"name": "app_forge", "title": "Koyorina", "version": "0.1.0"},
                                          "capabilities": {"experimentalApi": self.allow_generation}})
            await self.send({"method": "initialized", "params": {}})
        except BaseException:
            await self.__aexit__(None, None, None)
            raise
        return self

    async def _drain_stderr(self):
        """子プロセスの診断出力を捨てずに持つ。捨てると、落ちた理由が何も残らない。"""
        with suppress(Exception, asyncio.CancelledError):
            while line := await self.process.stderr.readline():
                self.stderr_tail.append(redacted(line.decode(errors="replace").rstrip()))

    def _report_exit(self):
        """こちらから閉じていないのに終わった場合だけ記録する。"""
        if self.closing:
            return
        logger.warning("codex_transport_closed %s", json.dumps(
            {"returncode": getattr(self.process, "returncode", None),
             "stderr": [line for line in self.stderr_tail if line]}, ensure_ascii=False))

    async def send(self, message):
        async with self.write_lock:
            self.process.stdin.write((json.dumps(message) + "\n").encode())
            await self.process.stdin.drain()

    async def _read(self):
        try:
            while line := await self.process.stdout.readline():
                message = json.loads(line)
                if "id" in message and "method" not in message:
                    future = self.pending.get(message["id"])
                    if future and not future.done():
                        if "error" in message:
                            # 利用者向けの文言は固定のまま、原因はサーバ側の記録にだけ残す。
                            error = message["error"] if isinstance(message.get("error"), dict) else {}
                            logger.warning("codex_rpc_error %s", json.dumps(
                                {"code": error.get("code"),
                                 "message": redacted(str(error.get("message", "")))}, ensure_ascii=False))
                            future.set_exception(RuntimeError("Codexが要求を受け付けませんでした。"))
                        else:
                            future.set_result(message.get("result", {}))
                elif "id" in message and message.get("method") == "item/tool/requestUserInput" and self.allow_user_input:
                    compact = self._compact_user_input(message)
                    if compact:
                        await self.notifications.put(compact)
                    else:
                        await self.send({"id": message["id"], "error": {"code": -32602, "message": "Invalid questions"}})
                elif "id" in message and message.get("method") == "item/tool/call" and self.allow_generation:
                    params = message.get("params") or {}
                    arguments = params.get("arguments")
                    if params.get("tool") == "app_forge_install_dependencies" and arguments in ({}, None):
                        await self.notifications.put({"method": "item/tool/call", "params": {
                            "requestId": message["id"], "threadId": params.get("threadId"),
                            "turnId": params.get("turnId"), "callId": params.get("callId")}})
                    else:
                        await self.send({"id": message["id"], "error": {
                            "code": -32602, "message": "Invalid dependency installation request"}})
                elif "id" in message:
                    # No approvals, user impersonation or credential refresh RPC. Work must fit the
                    # pre-authorized, network-disabled per-job sandbox or fail closed.
                    await self.send({"id": message["id"], "error": {"code": -32601, "message": "Not supported"}})
                elif message.get("method") == "item/agentMessage/delta":
                    # No raw fragments: a credential can span chunks.
                    p = message.get("params", {})
                    key = (p.get("threadId"), p.get("turnId"))
                    delta = p.get("delta", "")
                    if isinstance(delta, str):
                        self.response_bytes[key] = self.response_bytes.get(key, 0) + len(delta.encode())
                    if time.monotonic() - self.last_activity >= 2:
                        self.last_activity = time.monotonic()
                        if not self.notifications.full():
                            self.notifications.put_nowait({"method": "generation/activity", "params": {
                                "threadId": p.get("threadId"), "turnId": p.get("turnId"),
                                "responseBytes": self.response_bytes.get(key, 0)}})
                elif message.get("method") in {"item/started", "item/completed"}:
                    compact = self._compact_item_notification(message)
                    if compact:
                        await self.notifications.put(compact)
                elif message.get("method") == "turn/plan/updated":
                    await self.notifications.put(self._compact_plan_notification(message))
                elif message.get("method") in {"account/login/completed", "turn/completed"}:
                    await self.notifications.put(message)
                    if message.get("method") == "turn/completed":
                        p = message.get("params", {})
                        self.response_bytes.pop((p.get("threadId"), (p.get("turn") or {}).get("id")), None)
        except (Exception, asyncio.CancelledError):
            pass
        finally:
            self._report_exit()
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(CodexTransportClosed("Codex接続が終了しました。"))
            with suppress(asyncio.QueueFull):
                self.notifications.put_nowait({"method": "transport/closed"})

    @staticmethod
    def _compact_user_input(message):
        params = message.get("params") or {}
        questions = []
        for raw in (params.get("questions") or [])[:MAX_QUESTIONS]:
            if not isinstance(raw, dict):
                continue
            question_id = str(raw.get("id", ""))[:80]
            question = str(raw.get("question", ""))[:1000]
            if not question_id or not question:
                continue
            options = []
            for option in (raw.get("options") or [])[:MAX_OPTIONS]:
                if isinstance(option, dict) and option.get("label"):
                    options.append({"label": str(option["label"])[:120],
                                    "description": str(option.get("description", ""))[:500]})
            questions.append({"id": question_id, "header": str(raw.get("header", "確認"))[:40],
                              "question": question, "options": options,
                              "is_other": bool(raw.get("isOther", True))})
        if not questions:
            return None
        return {"method": "item/tool/requestUserInput", "params": {
            "requestId": message["id"], "threadId": params.get("threadId"),
            "turnId": params.get("turnId"), "questions": questions}}

    async def answer_user_input(self, request_id, answers):
        if (not self.allow_user_input or not isinstance(request_id, (int, str))
                or isinstance(request_id, str) and not 0 < len(request_id) <= 100):
            raise ValueError("回答を受け付けていません。")
        result = {str(key)[:80]: {"answers": [str(answer)[:1000] for answer in value[:MAX_OPTIONS]]}
                  for key, value in answers.items() if isinstance(value, list) and value}
        if not result:
            raise ValueError("回答を選択してください。")
        await self.send({"id": request_id, "result": {"answers": result}})

    async def answer_dynamic_tool(self, request_id, result):
        success = result.get("status") in {"installed", "unchanged"}
        message = ("依存関係を導入しました。ビルドとテストを続けてください。" if success else
                   "依存関係を導入できませんでした。manifestを確認し、同じ要求を繰り返さないでください。")
        await self.send({"id": request_id, "result": {"contentItems": [
            {"type": "inputText", "text": message}], "success": success}})

    @staticmethod
    def _compact_item_notification(message):
        params = message.get("params", {})
        item = params.get("item") or {}
        item_type = item.get("type")
        compact = {"type": item_type, "status": item.get("status")}
        if item_type == "agentMessage":
            text = item.get("text", "")
            compact.update({"phase": item.get("phase"),
                            "text": text[:16_000] if isinstance(text, str) else ""})
        elif item_type == "fileChange":
            compact["changes"] = [{"path": change.get("path"), "kind": change.get("kind")}
                                  for change in (item.get("changes") or [])[:50]
                                  if isinstance(change, dict)]
        elif item_type not in {"commandExecution", "plan"}:
            return None
        if item_type == "plan":
            text = item.get("text", "")
            compact["text"] = text[:8_000] if isinstance(text, str) else ""
        return {"method": message.get("method"), "params": {"threadId": params.get("threadId"),
                "turnId": params.get("turnId"), "item": compact}}

    @staticmethod
    def _compact_plan_notification(message):
        params = message.get("params", {})
        plan = [{"step": str(entry.get("step", ""))[:500], "status": entry.get("status")}
                for entry in (params.get("plan") or [])[:20] if isinstance(entry, dict)]
        return {"method": "turn/plan/updated", "params": {"threadId": params.get("threadId"),
                "turnId": params.get("turnId"), "plan": plan}}

    async def call(self, method, params):
        # 接続検証では認証状態の読み取りだけを許可する。
        allowed = {"initialize", "account/read", "model/list"}
        if self.allow_login:
            allowed |= {"account/login/start", "account/login/cancel", "account/logout"}
        if self.allow_generation:
            allowed |= {"thread/start", "turn/start", "turn/interrupt", "collaborationMode/list"}
        if method not in allowed:
            raise ValueError("この検証ブリッジでは許可されていない操作です。")
        if method == "account/login/start" and params.get("type") != "chatgptDeviceCode":
            raise ValueError("本人のChatGPTデバイス認証だけを利用できます。")
        self.sequence += 1
        request_id = self.sequence
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        try:
            await self.send({"id": request_id, "method": method, "params": params})
            return await asyncio.wait_for(future, 30)
        finally:
            self.pending.pop(request_id, None)

    async def wait_for_login(self, login_id):
        async with asyncio.timeout(600):
            while True:
                message = await self.notifications.get()
                if message.get("method") == "transport/closed":
                    raise RuntimeError("認証前にCodex接続が終了しました。")
                if message.get("method") == "account/login/completed":
                    result = message.get("params", {})
                    if result.get("loginId") == login_id:
                        return result.get("success") is True

    async def __aexit__(self, *args):
        self.closing = True
        if self.process and self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), 5)
            except asyncio.TimeoutError:
                self.process.kill()
                await self.process.wait()
        for task in (self.reader, self.stderr_task):
            if task:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
