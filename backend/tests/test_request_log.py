import logging

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from backend.core.request_log import RequestLogMiddleware, configure_logging, should_log

JOB = "0b8c2f5e-6d0a-4a55-9a53-4b8a4c4f8a11"
TENANT = "00000000-0000-4000-8000-000000000001"


@pytest.mark.parametrize("method, path, status, elapsed, logged", [
    ("POST", f"/tenants/{TENANT}/users/{JOB}/jobs/start", 202, 30, True),
    ("POST", f"/jobs/{JOB}/revalidate", 200, 10, True),
    ("GET", f"/tenants/{TENANT}/users/{JOB}/projects/{JOB}/files", 200, 10, True),
    ("GET", "/healthz", 200, 1, False),
    # 数秒おきの状態確認は、通常は出さない。
    ("GET", f"/jobs/{JOB}", 200, 10, False),
    ("GET", f"/tenants/{TENANT}/users/{JOB}/jobs/{JOB}/progress", 200, 10, False),
    ("GET", f"/users/{JOB}/runtimes", 200, 10, False),
    ("GET", f"/tenants/{TENANT}/projects/{JOB}/logs", 200, 10, False),
    # 失敗と遅延は出す。
    ("GET", f"/jobs/{JOB}", 404, 10, True),
    ("GET", f"/jobs/{JOB}/progress", 200, 2500, True),
    # 状態確認と同じパスでも、GET以外は操作なので出す。
    ("POST", f"/jobs/{JOB}/cancel", 200, 10, True),
])
def test_polling_is_quiet_unless_it_fails_or_is_slow(method, path, status, elapsed, logged):
    assert should_log(method, path, status, elapsed) is logged


def make_app():
    app = FastAPI()

    @app.get("/items/{item_id}")
    def item(item_id: str):
        return {"id": item_id}

    @app.post("/boom")
    def boom():
        raise HTTPException(503, "down")

    @app.get("/crash")
    def crash():
        raise RuntimeError("unexpected")

    app.add_middleware(RequestLogMiddleware)
    return app


def test_each_call_is_one_line_without_query_or_body(caplog):
    client = TestClient(make_app(), raise_server_exceptions=False)
    with caplog.at_level(logging.INFO, logger="koyorina.access"):
        client.get("/items/abc?path=secret/file.py", headers={"X-Forge-Auth": "token-value"})
        client.post("/boom", json={"text": "本文"})
        client.get("/crash")
    lines = [record.getMessage() for record in caplog.records if record.name == "koyorina.access"]
    assert lines[0].startswith("GET /items/abc 200 ") and lines[0].endswith("ms")
    assert lines[1].startswith("POST /boom 503 ")
    assert lines[2].startswith("GET /crash 500 ")
    # クエリ・ヘッダー・本文は出さない。
    assert not any(word in caplog.text for word in ("secret", "token-value", "本文"))
    levels = [record.levelno for record in caplog.records if record.name == "koyorina.access"]
    assert levels == [logging.INFO, logging.WARNING, logging.WARNING]


def test_koyorina_info_reaches_stdout_once(capsys):
    configure_logging()
    configure_logging()  # 何度呼ばれても出力先は1つ
    handlers = [h for h in logging.getLogger("koyorina").handlers if getattr(h, "koyorina", False)]
    assert len(handlers) == 1
    # 標準出力の差し替えに追随させる（テストの捕捉先へ向ける）。
    handlers[0].setStream(__import__("sys").stdout)
    logging.getLogger("koyorina.codex-controller").info("reaped_idle_agent example")
    assert capsys.readouterr().out.count("reaped_idle_agent example") == 1
