"""利用者と所属組織のマスター。役割でできることを分ける。"""
from datetime import datetime, timedelta, timezone

from backend.tests.test_api import context, login  # noqa: F401


def test_departments_are_managed_by_administrators_only(context):
    client, _ = context
    login(client)  # alice = admin
    created = client.post("/api/departments", json={"name": "経理・財務", "note": "部門"})
    assert created.status_code == 201, created.text
    department = created.json()
    assert client.post("/api/departments", json={"name": "経理・財務"}).status_code == 409

    renamed = client.patch(f"/api/departments/{department['id']}",
                           json={"name": "経理", "note": "部門", "enabled": True})
    assert renamed.json()["name"] == "経理"

    # 開発者は選ぶために一覧は見られるが、直せない。
    login(client, "bob@example.com")
    assert client.get("/api/departments").status_code == 200
    assert client.post("/api/departments", json={"name": "営業"}).status_code == 403
    assert client.get("/api/users").status_code == 403


def test_user_keeps_department_and_role(context):
    client, _ = context
    login(client)
    department = client.post("/api/departments", json={"name": "情報システム"}).json()
    created = client.post("/api/users", json={
        "email": "New@Example.com", "display_name": "TEST USER", "role": "member",
        "department_id": department["id"], "google_subject": "123456789012345678901"}).json()
    assert created["email"] == "new@example.com"  # 大文字小文字を揃えて保存する
    assert created["department_name"] == "情報システム" and created["role"] == "member"
    assert created["codex_enabled"] is True

    # 所属している人が残ったままの削除は止める。
    # 削除も他の更新と同じくJSONで送る。送信形式の検査を一つに保つ。
    assert client.request("DELETE", f"/api/departments/{department['id']}", json={}).status_code == 409

    moved = client.patch(f"/api/users/{created['id']}", json={
        "email": "new@example.com", "display_name": "TEST USER", "role": "admin",
        "department_id": None, "google_subject": ""}).json()
    assert moved["role"] == "admin" and moved["department_id"] is None
    assert moved["google_subject"] is None
    assert client.request("DELETE", f"/api/departments/{department['id']}", json={}).status_code == 200

    # 知らない役割は受け付けない。画面の選択肢とサーバーの一覧を揃える。
    assert client.patch(f"/api/users/{created['id']}", json={
        "email": "new@example.com", "role": "superuser"}).status_code == 422


def test_administrator_can_control_codex_and_view_ai_usage(context):
    client, sessions = context
    from backend.core.db import GenerationJob, Project, User
    from sqlalchemy import select
    login(client)
    bob = next(user for user in client.get("/api/users").json() if user["email"] == "bob@example.com")
    changed = client.patch(f"/api/users/{bob['id']}", json={
        "email": bob["email"], "role": bob["role"], "codex_enabled": False})
    assert changed.status_code == 200 and changed.json()["codex_enabled"] is False
    with sessions.begin() as db:
        user = db.scalar(select(User).where(User.email == "bob@example.com"))
        project = Project(owner_id=user.id, name="利用量確認", purpose="利用量を確認する",
                          audience="self", fields=[], status="approved", revision=1,
                          approved_revision=1)
        db.add(project); db.flush()
        started = datetime.now(timezone.utc) - timedelta(seconds=75)
        db.add(GenerationJob(project_id=project.id, owner_id=user.id, revision=1,
                             specification={}, provider="gemini", model="gemini-3.5-flash",
                             effort="medium", status="generated", total_tokens=120,
                             input_tokens=100, output_tokens=20, cached_tokens=10,
                             created_at=started, updated_at=started + timedelta(seconds=75)))
    usage = client.get("/api/ai-usage?days=30").json()
    row = next(row for row in usage["users"] if row["user_id"] == bob["id"])
    assert row["requests"] == 1 and row["gemini_requests"] == 1
    assert row["total_tokens"] == 120 and row["codex_enabled"] is False
    provider = next(item for item in usage["providers"] if item["provider"] == "gemini")
    assert provider["requests"] == 1 and provider["total_tokens"] == 120
    assert next(item for item in usage["providers"] if item["provider"] == "antigravity")["requests"] == 0
    assert usage["totals"]["duration_seconds"] == 75
    assert usage["projects"] == [{"project_id": project.id, "project_name": "利用量確認",
        "requests": 1, "failed": 0, "input_tokens": 100, "output_tokens": 20,
        "total_tokens": 120, "tokenized_requests": 1, "duration_seconds": 75}]
    assert sum(day["requests"] for day in usage["daily"]) == 1
    login(client, "bob@example.com")
    assert client.get("/api/me").json()["can_use_codex"] is False
    assert client.get("/api/ai-usage").status_code == 403


def test_administrator_cannot_drop_their_own_management(context):
    client, sessions = context
    login(client)
    me = client.get("/api/me").json()
    result = client.patch(f"/api/users/{me['id']}", json={"email": me["email"], "role": "member"})
    assert result.status_code == 409


def test_only_administrators_can_read_pod_logs(context, monkeypatch):
    client, _ = context
    monkeypatch.setattr("backend.api.masters.cluster_reader.available", lambda: True)

    async def fake_logs(namespace, pod, tail, *, namespaces):
        assert namespaces == ("koyorina", "koyorina-codex", "koyorina-preview")
        assert (namespace, pod, tail) == ("koyorina", "koyorina-123", 400)
        return {"namespace": namespace, "pod": pod, "lines": ["INFO ready", "ERROR failed"]}

    monkeypatch.setattr("backend.api.masters.cluster_reader.read_logs", fake_logs)
    login(client)
    response = client.get("/api/cluster/koyorina/pods/koyorina-123/logs")
    assert response.status_code == 200 and response.json()["lines"] == ["INFO ready", "ERROR failed"]
    login(client, "bob@example.com")
    assert client.get("/api/cluster/koyorina/pods/koyorina-123/logs").status_code == 403


def test_only_administrators_can_read_network_flows(context, monkeypatch):
    client, _ = context
    monkeypatch.setattr("backend.api.masters.cluster_reader.available", lambda: True)

    async def fake_flows(minutes, verdict):
        assert (minutes, verdict) == (60, "blocked")
        return {"available": True, "window_minutes": minutes, "observed": 1,
                "totals": {"allowed": 0, "blocked": 1, "reset": 0, "unknown": 0},
                "rows": [], "truncated": False}

    monkeypatch.setattr("backend.api.masters.cluster_reader.read_network_flows", fake_flows)
    login(client)
    result = client.get("/api/cluster/network/flows?minutes=60&verdict=blocked")
    assert result.status_code == 200 and result.json()["totals"]["blocked"] == 1
    login(client, "bob@example.com")
    assert client.get("/api/cluster/network/flows").status_code == 403


def test_only_developers_and_administrators_can_create_applications(context):
    client, sessions = context
    from backend.core.db import User
    from sqlalchemy import select
    login(client)
    with sessions.begin() as db:
        db.scalar(select(User).where(User.email == "bob@example.com")).role = "user"
    login(client, "bob@example.com")
    refused = client.post("/api/projects", json={"name": "台帳", "purpose": "備品を管理する",
        "audience": "self", "fields": [{"name": "品名", "kind": "text", "required": True}]})
    assert refused.status_code == 403
    assert "開発" in refused.json()["error"]
