"""テナント管理者。自分が管理者のテナントの生成AIだけを扱える。"""
from sqlalchemy import select

from backend.core.db import Tenant, User
from backend.tests.test_api import context, login  # noqa: F401
from backend.tests.test_tenant_ai import VERTEX


def two_tenants(sessions):
    with sessions() as db:
        first, second = Tenant(name="製造"), Tenant(name="管理部")
        db.add_all([first, second])
        db.commit()
        bob = db.scalar(select(User).where(User.email == "bob@example.com"))
        return first.id, second.id, bob.id


def membership(tenant_id, role):
    return {"tenant_id": tenant_id, "role": role}


def test_system_admins_set_a_role_for_each_tenant(context):
    client, sessions = context
    first, second, bob = two_tenants(sessions)
    login(client)
    body = {"email": "bob@example.com", "role": "member",
            "tenants": [membership(first, "admin"), membership(second, "owner")]}
    # テナントのロールは一覧にあるものだけ。同じテナントを二重に書くこともできない。
    assert client.patch(f"/api/users/{bob}", json=body).status_code == 422
    body["tenants"] = [membership(first, "admin"), membership(first, "user")]
    assert client.patch(f"/api/users/{bob}", json=body).status_code == 422
    body["tenants"] = [membership(first, "admin"), membership(second, "user")]
    updated = client.patch(f"/api/users/{bob}", json=body).json()
    assert sorted(updated["tenants"], key=lambda item: item["tenant_id"]) == sorted(
        body["tenants"], key=lambda item: item["tenant_id"])
    listed = next(user for user in client.get("/api/users").json() if user["id"] == bob)
    assert {item["tenant_id"]: item["role"] for item in listed["tenants"]} == {first: "admin", second: "user"}
    # 所属から外すと、そのテナントでのロールも消える。
    body["tenants"] = [membership(second, "developer")]
    assert client.patch(f"/api/users/{bob}", json=body).json()["tenants"] == [membership(second, "developer")]


def test_tenant_admins_manage_only_their_own_tenants(context):
    client, sessions = context
    first, second, bob = two_tenants(sessions)
    login(client)
    client.patch(f"/api/users/{bob}", json={"email": "bob@example.com", "role": "member",
                                            "tenants": [membership(first, "admin"),
                                                        membership(second, "developer")]})
    login(client, "bob@example.com")
    me = client.get("/api/me").json()
    assert me["admin_tenant_ids"] == [first] and me["can_manage_users"] is False
    assert me["tenant_roles"] == {first: "admin", second: "developer"}
    assert client.get(f"/api/tenants/{first}/ai-settings").status_code == 200
    assert client.put(f"/api/tenants/{first}/ai-settings", json=VERTEX).status_code == 200
    # 所属していても、管理者でないテナントは触れない。マスター管理も使えない。
    assert client.get(f"/api/tenants/{second}/ai-settings").status_code == 403
    assert client.get("/api/users").status_code == 403


def test_system_admins_manage_every_tenant(context):
    client, sessions = context
    first, second, _ = two_tenants(sessions)
    login(client)
    assert set(client.get("/api/me").json()["admin_tenant_ids"]) >= {first, second}
    assert client.get(f"/api/tenants/{second}/ai-settings").status_code == 200


def test_tenant_admins_see_usage_only_for_their_tenants(context):
    from backend.core.db import GenerationJob, Project
    client, sessions = context
    first, second, bob = two_tenants(sessions)
    with sessions() as db:
        owner = db.scalar(select(User).where(User.email == "alice@example.com")).id
        for tenant, name in ((first, "製造のアプリ"), (second, "管理部のアプリ")):
            project = Project(owner_id=owner, tenant_id=tenant, name=name, purpose="確認用", audience="self",
                              fields=[], tables=[], requirements=[])
            db.add(project)
            db.flush()
            db.add(GenerationJob(project_id=project.id, owner_id=owner, revision=1, specification={},
                                 instruction="作成", status="generated", provider="claude", total_tokens=100,
                                 input_tokens=80, output_tokens=20))
        db.commit()
    login(client)
    client.patch(f"/api/users/{bob}", json={"email": "bob@example.com", "role": "member",
                                            "tenants": [membership(first, "admin")]})
    assert client.get("/api/ai-usage").json()["totals"]["requests"] == 2
    login(client, "bob@example.com")
    usage = client.get("/api/ai-usage").json()
    assert usage["totals"]["requests"] == 1 and usage["totals"]["claude_requests"] == 1
    assert [row["tenant_id"] for row in usage["tenants"]] == [first]
