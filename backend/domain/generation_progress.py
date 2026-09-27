"""Bounded, sanitized progress snapshots; never persist raw RPC notifications."""
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import re

logger = logging.getLogger("uvicorn.error")
SECRET_PATTERN = (r'-----BEGIN|\b(?:Bearer|sk-[\w-]+|AIza[\w-]+|eyJ[\w.-]+)|'
                  r'(?i:password|secret|token|api[_ -]?key|authorization|パスワード|秘密鍵)\s*[=:：]')
# 長い記号列（トークンやキーらしいもの）を伏せる。「/」は区切りとして扱い、
# frontend/src/components/… のようなファイルのパスは伏せない。トークンやキーは数字が混ざるので、
# 数字を含むものだけを伏せる（DataUploadPreviewPanelWithMapping のような長い部品名は残す）。
IDENTIFIER = r'(?<![A-Za-z0-9_+=-])(?=[A-Za-z0-9_+=-]*[0-9])[A-Za-z0-9_+=-]{32,}'
FENCE = re.compile(r"```.*?(?:```|\Z)", re.DOTALL)
# 件名と見送りの申告。画面の報告には要らない（別の形で出している）。
MARKER_LINE = re.compile(r"^\s*(?:COMMIT|NEXT)\s*[:：].*$", re.MULTILINE)


def safe_commentary(text: str) -> str:
    # Inspect the whole completed message, not individual token fragments.
    if len(text) > 8000 or re.search(r'```|[{}]|' + SECRET_PATTERN, text):
        return "AppGenの作業報告を受信しました（コード・機密情報の可能性があるため本文を非表示）。"
    text = re.sub(r'https?://\S+', '[URL省略]', text)
    text = re.sub(r'[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}', '[メール非表示]', text)
    text = re.sub(IDENTIFIER, '[識別子非表示]', text)
    text = re.sub(r'[\x00-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]', ' ', text)
    return text.strip()[:2000]


def safe_report(text: str) -> str:
    """モデルの報告を、段落と箇条書きを残したまま読める形にする。

    safe_commentary は1行に畳み、コードらしいものがあると全体を伏せる。作業の報告は
    たいてい手順やコマンドを ``` で囲んだ部分を含むので、それだと最後の報告がほぼ毎回
    「非表示」になる。ここではコードの塊だけを省き、残りの文章は出す。
    秘密らしい書き方や、コードそのもの（波括弧だらけ）は、これまでどおり全体を伏せる。
    """
    text = FENCE.sub("（コードは省略しました）", text or "")
    if len(text) > 20000 or re.search(SECRET_PATTERN, text) or text.count("{") + text.count("}") > 8:
        return "AppGenの作業報告を受信しました（コード・機密情報の可能性があるため本文を非表示）。"
    text = re.sub(r'https?://\S+', '[URL省略]', text)
    text = re.sub(r'[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}', '[メール非表示]', text)
    text = re.sub(IDENTIFIER, '[識別子非表示]', text)
    # 改行とタブは残す。段落と箇条書きが報告の読みやすさそのもの。
    text = re.sub(r'[\x00-\x08\x0b-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]', ' ', text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()[:6000]


def report_body(text: str) -> str:
    """画面に出す最後の報告。件名（COMMIT:）と見送り（NEXT:）の行は抜く。"""
    return re.sub(r"\n{3,}", "\n\n", MARKER_LINE.sub("", text or "")).strip()


class Progress:
    def __init__(self, folder: Path, job_id: str):
        self.path = folder / "progress.json"
        self.job_id = job_id
        self.data = {"events": [], "last_response_at": None, "response_bytes": 0, "truncated": False}
        self.sequence = 0

    def record(self, kind, message, *, response=False, response_bytes=None, key=None, state=None):
        """1件を記録する。`key` を渡すと同じ対象の既存行を書き換える。

        同じ作業の開始と完了を別々の行にすると、同じ文言が並んで読めなくなる。
        進行中の作業計画やローカル検査は、1行が状態を変えていく形にする。
        """
        now = datetime.now(timezone.utc).isoformat()
        if response:
            self.data["last_response_at"] = now
        if isinstance(response_bytes, int) and response_bytes >= self.data["response_bytes"]:
            self.data["response_bytes"] = response_bytes
        existing = next((e for e in reversed(self.data["events"]) if key and e.get("key") == key), None)
        if existing is not None:
            existing.update({"at": now, "message": message, "state": state})
            # 書き換えた行は末尾へ動かす。元の位置に残すと、時刻だけ新しい行が
            # 古い行の上に居ることになり、上から読めなくなる（応答待ちの経過が
            # 開始直後の位置で増え続け、その下に後の出来事が並んでいた）。
            if self.data["events"][-1] is not existing:
                self.data["events"].remove(existing)
                self.data["events"].append(existing)
        else:
            self.sequence += 1
            event = {"id": self.sequence, "at": now, "kind": kind, "message": message,
                     "key": key, "state": state}
            if kind == "activity" and self.data["events"] and self.data["events"][-1]["kind"] == "activity":
                # Keep commentary readable during long source-code streams.
                event["id"] = self.data["events"][-1]["id"]
                self.data["events"][-1] = event
            else:
                self.data["events"].append(event)
        if len(self.data["events"]) > 200:
            self.data["events"] = self.data["events"][-200:]
            self.data["truncated"] = True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.data, ensure_ascii=False))
        temporary.replace(self.path)
        logger.info("codex_progress %s", json.dumps(
            {"job_id": self.job_id, "kind": kind, "message": message, "state": state}, ensure_ascii=False))

    @classmethod
    def resume(cls, folder: Path, job_id: str) -> "Progress":
        """終わったジョブの記録へ書き足す。新しく作ると、それまでの経過が消える。"""
        progress = cls(folder, job_id)
        progress.data = cls.read(folder)
        progress.sequence = max((event.get("id", 0) for event in progress.data["events"]), default=0)
        return progress

    @staticmethod
    def read(folder):
        path = folder / "progress.json"
        if not path.is_file():
            return {"events": [], "last_response_at": None, "response_bytes": 0, "truncated": False}
        result = json.loads(path.read_text())
        result.setdefault("response_bytes", 0)
        return result


PLAN_MARKS = {"completed": "完了", "inProgress": "作業中", "pending": "未着手"}


def plan_summary(steps) -> str:
    """作業計画を1行ずつの一覧にする。状態だけを示し、内容は要約しない。"""
    lines = []
    for entry in steps[:12]:
        if not isinstance(entry, dict):
            continue
        step = safe_commentary(str(entry.get("step", "")))[:120]
        if step:
            lines.append(f"[{PLAN_MARKS.get(entry.get('status'), '　')}] {step}")
    done = sum(1 for entry in steps if isinstance(entry, dict) and entry.get("status") == "completed")
    header = f"作業計画（{done}/{len(steps)} 完了）" if steps else "作業計画"
    return "\n".join([header, *lines])


def command_exit(item) -> str:
    """終了コードがあれば添える。「完了しませんでした」だけでは手掛かりが無い。"""
    for key in ("exitCode", "exit_code", "status_code"):
        value = item.get(key)
        if isinstance(value, int):
            return f"終了 {value}"
    return ""


def command_label(item) -> str:
    """実行したプログラム名だけを取り出す。引数・出力は進捗に含めない。"""
    raw = (item.get("command") or item.get("commandLine") or item.get("argv")
           or item.get("cmd") or item.get("parsedCmd") or "")
    if isinstance(raw, list):
        raw = raw[0] if raw else ""
    first = str(raw).strip().split()[0] if str(raw).strip() else ""
    name = first.rsplit("/", 1)[-1]
    return name if re.fullmatch(r"[A-Za-z0-9._+-]{1,40}", name) else ""
