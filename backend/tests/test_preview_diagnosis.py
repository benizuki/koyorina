"""プレビューが起動しなかった理由を、ログを読まない人へ伝えられるか。"""
import asyncio
from types import SimpleNamespace
import pytest
from backend.api import preview as api
from backend.domain.preview_diagnosis import diagnose

PROJECT = "49913f18-b465-42da-9c21-e1f0a2dd103e"
TRACEBACK = ('Traceback (most recent call last):\n'
             '  File "/workspace/backend/main.py", line 3, in <module>\n'
             '    import httpx\n'
             "ModuleNotFoundError: No module named 'httpx'\n")


@pytest.mark.parametrize("logs,expected,evidence", [
    (TRACEBACK, "httpx", "ModuleNotFoundError"),
    ("Error: Cannot find module 'vue-router'\n", "vue-router", "Cannot find module"),
    ('  File "/workspace/app.py", line 4\nSyntaxError: invalid syntax\n', "文法", "SyntaxError"),
    ("sqlite3.OperationalError: no such table: records\n", "records", "no such table"),
    ("KeyError: 'API_BASE'\n", "API_BASE", "KeyError"),
    ("ERROR: [Errno 98] Address already in use\n", "ポート", "Address already in use"),
    ("Killed\nexit code 137\n", "メモリ", "137"),
])
def test_known_failures_are_named_in_plain_japanese(logs, expected, evidence):
    result = diagnose(logs)
    assert expected in result["message"]
    # 次にやることまで書く。「ログを確認してください」では読めない人がそこで詰まる。
    assert result["hint"]
    assert evidence in result["evidence"]


def test_an_unknown_failure_still_hands_over_the_line_to_paste():
    result = diagnose("INFO: booting\nRuntimeError: something odd happened\n")
    assert result["evidence"] == "RuntimeError: something odd happened"
    assert result["hint"]


def test_quiet_and_empty_logs_do_not_invent_a_cause():
    for logs, expect_evidence in (("INFO: booting\nINFO: done\n", False), ("", False), (None, False)):
        result = diagnose(logs)
        assert result["message"] and result["hint"]
        assert bool(result["evidence"]) is expect_evidence


def test_the_most_recent_attempt_is_the_one_explained():
    """起動し直しを繰り返すと同じログが積み上がる。直近の失敗を根拠にする。"""
    logs = TRACEBACK + TRACEBACK.replace("httpx", "pandas")
    assert "pandas" in diagnose(logs)["message"]


class FakeRunner:
    def __init__(self, state, logs=""):
        self.state, self.output, self.log_calls = state, logs, 0

    async def status(self, project_id):
        return self.state

    async def logs(self, project_id):
        self.log_calls += 1
        if isinstance(self.output, Exception):
            raise self.output
        return self.output


def snapshot(monkeypatch, runner):
    monkeypatch.setattr(api, "backend", lambda settings: runner)
    return asyncio.run(api.snapshot(SimpleNamespace(preview_enabled=True, app_origin="https://forge.test", apps_suffix="forge.test"), PROJECT))


def failed_state(**overrides):
    return {"state": "failed", "port": None, "job_id": "a5811e7e-a100-41d3-9100-0412e340ca68",
            "updated_at": None, "message": "プレビューが停止しました。", **overrides}


def test_a_failed_preview_returns_the_cause_the_hint_and_the_evidence(monkeypatch):
    runner = FakeRunner(failed_state(), logs=TRACEBACK)
    result = snapshot(monkeypatch, runner)
    assert result["state"] == "failed"
    assert "httpx" in result["message"] and result["hint"]
    assert "ModuleNotFoundError" in result["evidence"]


def test_a_running_preview_does_not_read_the_logs(monkeypatch):
    runner = FakeRunner({"state": "running", "port": 8081, "job_id": None, "updated_at": None,
                         "message": None})
    result = snapshot(monkeypatch, runner)
    assert result["state"] == "running" and result["url"]
    assert result["hint"] is None and result["evidence"] is None
    assert runner.log_calls == 0


def test_unreadable_logs_still_report_the_failure(monkeypatch):
    runner = FakeRunner(failed_state(), logs=RuntimeError("log backend down"))
    result = snapshot(monkeypatch, runner)
    # 理由が分からないことと、状態が分からないことは別。失敗したことは必ず伝える。
    assert result["state"] == "failed" and result["message"]
    assert "実行ログを取得できませんでした" in result["hint"]
