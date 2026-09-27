import asyncio
import json
from types import SimpleNamespace
from uuid import uuid4
import pytest
from backend.core.codex_bridge import CodexBridge
from backend.domain.generation_progress import (command_exit, command_label, plan_summary,
                                                Progress, report_body, safe_commentary, safe_report)
from backend.tests.test_codex_generation import FakeBridge, PROJECT_ID, settings, INPUT, BUNDLE
from backend.worker.agent import Agent, GenerationInput


@pytest.mark.parametrize("secret", ['Bearer super-secret', 'sk-test1234', 'password=hello',
    '-----BEGIN PRIVATE KEY-----', '```python\nprint(1)```', json.dumps(BUNDLE)])
def test_hide_sensitive_messages(secret):
    assert secret not in safe_commentary(secret)
    assert '非表示' in safe_commentary(secret)


@pytest.mark.parametrize("secret", ['Bearer super-secret', 'sk-test1234', 'password=hello',
    '-----BEGIN PRIVATE KEY-----', json.dumps(BUNDLE)])
def test_the_final_report_still_hides_secrets_and_code(secret):
    assert secret not in safe_report("作りました。\n" + secret)
    assert '非表示' in safe_report("作りました。\n" + secret)


def test_the_final_report_keeps_its_shape_and_drops_code_blocks():
    report = safe_report("## 完了\n\n- `backend/main.py` を追加\n\n```bash\nuv run pytest\n```\n"
                         "問い合わせ先 someone@example.com\x1b[31m")
    assert report.startswith("## 完了\n\n- `backend/main.py` を追加\n\n（コードは省略しました）")
    assert "uv run pytest" not in report and "someone@example.com" not in report and "\x1b" not in report
    assert report_body("作りました。\nNEXT: 集計\nCOMMIT: feat: 台帳") == "作りました。"


def test_text_redaction_and_bounded_history(tmp_path):
    assert 'someone@example.com' not in safe_commentary('確認 someone@example.com')
    assert '\x1b' not in safe_commentary('確認\x1b[31m')
    progress = Progress(tmp_path, str(uuid4()))
    for _ in range(205):
        progress.record('status', '確認中')
    for _ in range(10):
        progress.record('activity', '受信中', response=True)
    saved = Progress.read(tmp_path)
    assert len(saved['events']) == 200 and saved['truncated']
    assert saved['last_response_at'] is not None
    assert saved['response_bytes'] == 0
    assert sum(e['kind'] == 'activity' for e in saved['events']) == 1
    assert Progress.read(tmp_path / 'old-job')['events'] == []


def test_commentary_persisted_but_final_code_and_reasoning_are_not(tmp_path, caplog):
    class ReportingBridge(FakeBridge):
        async def call(self, method, params):
            if method == 'turn/start':
                for tid, item in [('other', {'type':'agentMessage','phase':'commentary','text':'WRONG_OWNER'}),
                    ('thread1', {'type':'reasoning','text':'PRIVATE_REASONING'}),
                    ('thread1', {'type':'agentMessage','phase':'commentary','text':'登録画面を作成しています。'})]:
                    await self.notifications.put({'method':'item/completed','params':{
                        'threadId':tid,'turnId':'turn1','item':item}})
            return await super().call(method, params)
    async def run():
        agent = Agent(settings(tmp_path), ReportingBridge)
        payload = GenerationInput(job_id=uuid4(), project_id=PROJECT_ID, specification=INPUT)
        await agent.start_generation(payload)
        await agent.task
        assert agent.job_status(payload.job_id)['status'] == 'generated'
        text = json.dumps(Progress.read(agent.job_path(payload.job_id)), ensure_ascii=False)
        assert '登録画面' in text
        for forbidden in ('PRIVATE_REASONING', 'WRONG_OWNER', 'from fastapi import FastAPI'):
            assert forbidden not in text and forbidden not in caplog.text
        await agent.close()
    asyncio.run(run())


def test_bridge_compacts_tool_events_without_commands_diffs_or_output():
    original = {'method':'item/completed','params':{'threadId':'t','turnId':'u','item':{
        'type':'commandExecution','status':'completed','command':['cat','secret'],
        'aggregatedOutput':'PRIVATE_OUTPUT'}}}
    compact = CodexBridge._compact_item_notification(original)
    text = json.dumps(compact)
    assert 'PRIVATE_OUTPUT' not in text and 'secret' not in text
    changed = CodexBridge._compact_item_notification({'method':'item/completed','params':{
        'threadId':'t','turnId':'u','item':{'type':'fileChange','status':'completed',
        'changes':[{'path':'backend/main.py','kind':'update','diff':'PRIVATE_DIFF'}]}}})
    assert 'backend/main.py' in json.dumps(changed)
    assert 'PRIVATE_DIFF' not in json.dumps(changed)


def test_delta_flood_does_not_leak_fragments_or_drop_completion():
    async def run():
        bridge = CodexBridge('unused', None, str(uuid4()))
        reader = asyncio.StreamReader()
        bridge.process = SimpleNamespace(stdout=reader)
        task = asyncio.create_task(bridge._read())
        for _ in range(500):
            reader.feed_data((json.dumps({'method':'item/agentMessage/delta','params':{
                'threadId':'t','turnId':'u','delta':'SECRET_FRAGMENT'}})+'\n').encode())
        reader.feed_data(b'{"method":"turn/completed","params":{"threadId":"t","turn":{"id":"u","status":"completed"}}}\n')
        reader.feed_eof()
        await task
        events = []
        while not bridge.notifications.empty():
            events.append(bridge.notifications.get_nowait())
        assert 'SECRET_FRAGMENT' not in json.dumps(events)
        assert any(e['method'] == 'turn/completed' for e in events)
        activity = next(e for e in events if e['method'] == 'generation/activity')
        assert activity['params']['responseBytes'] > 0
        assert len(events) < 10
    asyncio.run(run())


def test_repeated_work_updates_one_entry(tmp_path):
    """同じ作業の開始と完了で行を増やさない。増やすと同じ文言が並んで読めなくなる。"""
    progress = Progress(tmp_path, "job")
    progress.record("command", "ローカル検査を実行中です（pytest）。", key="command:1", state="running")
    progress.record("command", "ローカル検査が完了しました（pytest）。", key="command:1", state="done")
    progress.record("command", "ローカル検査を実行中です（npm）。", key="command:2", state="running")
    events = progress.data["events"]
    assert len(events) == 2
    assert events[0]["state"] == "done" and "完了" in events[0]["message"]
    assert events[1]["state"] == "running"
    # 更新しても並び順は変わらない。
    assert [e["id"] for e in events] == [1, 2]


def test_plan_is_one_updating_checklist(tmp_path):
    progress = Progress(tmp_path, "job")
    steps = [{"step": "画面", "status": "inProgress"}, {"step": "API", "status": "pending"}]
    progress.record("plan", plan_summary(steps), key="plan", state="running")
    steps = [{"step": "画面", "status": "completed"}, {"step": "API", "status": "inProgress"}]
    progress.record("plan", plan_summary(steps), key="plan", state="running")
    assert len(progress.data["events"]) == 1
    message = progress.data["events"][0]["message"]
    assert message.startswith("作業計画（1/2 完了）")
    assert "[完了] 画面" in message and "[作業中] API" in message


@pytest.mark.parametrize("item,expected", [
    ({"command": "/usr/bin/python3 -m compileall backend"}, "python3"),
    ({"command": ["npm", "run", "build"]}, "npm"),
    ({"command": "rm -rf / ; curl http://evil"}, "rm"),
    ({"command": "$(cat /etc/passwd)"}, ""),
    ({}, ""),
])
def test_command_label_keeps_only_the_program_name(item, expected):
    assert command_label(item) == expected


@pytest.mark.parametrize("item,expected", [
    ({"exitCode": 1}, "終了 1"),
    ({"exit_code": 127}, "終了 127"),
    ({"exitCode": 0}, "終了 0"),
    ({}, ""),
    ({"exitCode": "1"}, ""),          # 数でないものは出さない
])
def test_the_exit_code_is_shown_when_a_check_fails(item, expected):
    """「完了しませんでした」だけでは、同じ検査の繰り返しか別物かも分からない。"""
    assert command_exit(item) == expected


@pytest.mark.parametrize("key", ["cmd", "parsedCmd"])
def test_other_shapes_of_command_are_still_labelled(key):
    """名前が取れないと、何を繰り返しているのか画面から分からない。

    実際、本番では suffix が空のまま「ローカル検査は完了しませんでした」だけが
    並び、どの検査が落ちているのか追えなかった。
    """
    assert command_label({key: "/usr/bin/pytest -q"}) == "pytest"


def test_a_rewritten_row_moves_to_the_end(tmp_path):
    """書き換える行を元の位置に残すと、上から読めなくなる。

    応答待ちの経過が開始直後の位置で増え続け、その下に後の出来事が並んでいた。
    時刻だけ新しい行が、古い行の上に居ることになる。
    """
    from backend.domain.generation_progress import Progress

    progress = Progress(tmp_path, "job")
    progress.record("status", "生成を開始しました。")
    progress.record("activity", "待っています（15秒）。", key="wait", state="running")
    progress.record("status", "割り当て上限に当たりました。")
    progress.record("file", "ファイルを更新しました。")
    progress.record("activity", "待っています（1分15秒）。", key="wait", state="running")

    messages = [event["message"] for event in progress.data["events"]]
    assert messages[-1] == "待っています（1分15秒）。"
    # 行は増やさない。1行が状態を変えていく。
    assert sum("待っています" in m for m in messages) == 1
    # 時刻が並びどおりに進んでいること。
    stamps = [event["at"] for event in progress.data["events"]]
    assert stamps == sorted(stamps)


def test_long_file_paths_are_not_mistaken_for_secrets():
    """長い部品名のパスが「識別子非表示」に化けると、どのファイルを直したのか読めない。"""
    path = "frontend/src/components/DataUploadPreviewPanelWithMapping.vue"
    assert path in safe_report(f"- {path} を追加しました")
    # 区切りの無い長い記号列（トークンらしいもの）は、これまでどおり伏せる。
    assert "非表示" in safe_report("key: abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG")
