"""Tenant authorization and physical-storage routing boundary."""
import pytest
from backend.core.db import Project, ProjectCollaborator, Tenant, User, UserTenant
from backend.tests.test_collaborators import context, login, share  # noqa: F401
from backend.worker.controller import ControllerSettings, resources
from uuid import uuid4


def make_tenant(app, name, members=(), role="developer"):
    with app.state.sessions.begin() as db:
        tenant = Tenant(name=name)
        db.add(tenant)
        db.flush()
        for email in members:
            member = db.query(User).filter_by(email=email).one()
            db.add(UserTenant(user_id=member.id, tenant_id=tenant.id, role=role))
        return tenant.id


def test_an_app_is_created_in_the_tenant_its_owner_belongs_to(context):
    client, app, (pid, _, owner_id, _) = context
    sales = make_tenant(app, "営業", ["owner@example.com"])
    login(client, "owner@example.com")
    created = client.post(f"/api/projects?tenant_id={sales}", json={"name": "台帳", "purpose": "貸出を管理する",
                                                 "audience": "team",
                                                 "fields": [{"name": "品名", "kind": "text",
                                                             "required": True}]})
    assert created.status_code == 201 and created.json()["tenant_id"] == sales


def test_only_a_developer_in_that_tenant_may_create_there(context):
    """アプリを作れるかはテナントごと。別のテナントで開発者でも、利用者のテナントには作れない。"""
    client, app, _ = context
    sales = make_tenant(app, "営業", ["owner@example.com"], role="user")
    factory = make_tenant(app, "製造", ["owner@example.com"], role="developer")
    login(client, "owner@example.com")
    spec = {"name": "台帳", "purpose": "貸出を管理する", "audience": "team",
            "fields": [{"name": "品名", "kind": "text", "required": True}]}
    assert client.post(f"/api/projects?tenant_id={sales}", json=spec).status_code == 403
    assert client.post(f"/api/projects?tenant_id={factory}", json=spec).status_code == 201
    me = client.get("/api/me").json()
    assert factory in me["develop_tenant_ids"] and sales not in me["develop_tenant_ids"]
    assert me["tenant_roles"][sales] == "user" and me["can_develop"] is True


def test_an_app_cannot_be_put_into_a_tenant_you_do_not_belong_to(context):
    client, app, (pid, _, _, _) = context
    make_tenant(app, "営業", ["owner@example.com"])
    other = make_tenant(app, "製造")
    login(client, "owner@example.com")
    refused = client.post(f"/api/projects?tenant_id={other}",
                          json={"name": "台帳", "purpose": "貸出を管理する", "audience": "team",
                                "fields": [{"name": "品名", "kind": "text", "required": True}]})
    assert refused.status_code == 403


def test_a_user_without_membership_cannot_create_a_project(context):
    client, app, (pid, _, _, _) = context
    only = make_tenant(app, "未所属テナント")
    with app.state.sessions.begin() as db:
        owner = db.query(User).filter_by(email="owner@example.com").one()
        db.query(UserTenant).filter_by(user_id=owner.id).delete()
    login(client, "owner@example.com")
    created = client.post("/api/projects", json={"name": "台帳", "purpose": "貸出を管理する",
                                                 "audience": "team",
                                                 "fields": [{"name": "品名", "kind": "text",
                                                             "required": True}]})
    # どのテナントでも開発者でないので、置き場を選ぶ前に断る。
    assert created.status_code == 403


def test_sharing_does_not_cross_tenants(context):
    client, app, (pid, _, owner_id, mate_id) = context
    make_tenant(app, "営業", ["owner@example.com"])
    make_tenant(app, "製造", ["mate@example.com"])
    share(app, pid, mate_id, owner_id)
    with app.state.sessions.begin() as db:
        sales = db.query(Tenant).filter_by(name="営業").one()
        db.get(Project, pid).tenant_id = sales.id
    login(client, "mate@example.com")
    assert client.get("/api/projects").json() == []
    assert client.post(f"/api/projects/{pid}/approve", json={"revision": 1}).status_code == 404


def test_removed_tenant_membership_blocks_direct_owner_ids(context):
    client, app, (pid, _, owner_id, _) = context
    with app.state.sessions.begin() as db:
        db.query(UserTenant).filter_by(user_id=owner_id).delete()
    login(client, "owner@example.com")
    assert client.get(f"/api/projects/{pid}/collaborators/candidates").status_code == 404
    assert client.request("DELETE", f"/api/projects/{pid}", json={}).status_code == 404


def test_direct_tenant_changes_are_rejected(context):
    client, app, (pid, _, owner_id, mate_id) = context
    make_tenant(app, "営業", ["owner@example.com", "mate@example.com"])
    factory = make_tenant(app, "製造", ["owner@example.com", "mate@example.com"])
    share(app, pid, mate_id, owner_id)
    login(client, "mate@example.com")
    assert client.post(f"/api/projects/{pid}/tenant", json={"tenant_id": factory}).status_code == 404
    login(client, "owner@example.com")
    moved = client.post(f"/api/projects/{pid}/tenant", json={"tenant_id": factory})
    assert moved.status_code == 409


def test_a_tenant_with_apps_is_not_deleted(context):
    """区分が消えると、そのアプリの利用量をどこにも数えられなくなる。"""
    client, app, (pid, _, _, _) = context
    sales = make_tenant(app, "営業", ["owner@example.com"])
    with app.state.sessions.begin() as db:
        db.get(Project, pid).tenant_id = sales
    login(client, "admin@example.com")
    assert client.request("DELETE", f"/api/tenants/{sales}", json={}).status_code == 409


def test_usage_is_counted_per_tenant(context):
    client, app, (pid, jid, _, _) = context
    sales = make_tenant(app, "営業", ["owner@example.com"])
    with app.state.sessions.begin() as db:
        db.get(Project, pid).tenant_id = sales
    login(client, "admin@example.com")
    usage = client.get("/api/ai-usage").json()
    rows = {row["tenant_name"]: row for row in usage["tenants"]}
    assert rows["営業"]["requests"] == 1 and rows["営業"]["projects"] == 1


def test_a_user_belongs_to_several_tenants(context):
    client, app, (_, _, _, mate_id) = context
    sales, factory = make_tenant(app, "営業"), make_tenant(app, "製造")
    login(client, "admin@example.com")
    updated = client.patch(f"/api/users/{mate_id}", json={
        "email": "mate@example.com", "display_name": "Mate", "role": "member",
        "tenants": [{"tenant_id": sales, "role": "developer"}, {"tenant_id": factory, "role": "user"}]})
    assert sorted(updated.json()["tenant_ids"]) == sorted([sales, factory])
    # 外すときも指定どおりに揃える。
    trimmed = client.patch(f"/api/users/{mate_id}", json={
        "email": "mate@example.com", "display_name": "Mate", "role": "member",
        "tenants": [{"tenant_id": factory, "role": "developer"}]})
    assert trimmed.json()["tenants"] == [{"tenant_id": factory, "role": "developer"}]


def test_the_screen_is_told_which_tenants_it_may_offer(context):
    """画面は選べるものだけを出す。選べない選択肢を並べると、押してから断られる。"""
    client, app, (_, _, _, mate_id) = context
    sales = make_tenant(app, "営業", ["mate@example.com"])
    make_tenant(app, "製造")
    login(client, "mate@example.com")
    assert sales in client.get("/api/me").json()["tenant_ids"]
    # 一覧そのものは誰でも引ける。名前を出すために要る。
    assert len(client.get("/api/tenants").json()) == 3


def test_workspace_claim_is_deterministic_and_tenant_specific():
    base = {"token": "x" * 40,
            "agent_image": "registry.example.com/koyorina-agent@sha256:" + "a" * 64}
    settings = ControllerSettings(**base)
    tenant = uuid4()
    pod = resources(uuid4(), settings, "codex", tenant=str(tenant))["pods"]["spec"]
    claims = {v["persistentVolumeClaim"]["claimName"] for v in pod["volumes"]
              if "persistentVolumeClaim" in v}
    assert len([claim for claim in claims if claim.startswith("koyorina-generation-")]) == 1


def test_the_list_carries_the_tenant_so_the_screen_can_filter(context):
    """画面は返ってきた tenant_id で絞る。持っていなければ絞りようがない。"""
    client, app, (pid, _, _, _) = context
    sales = make_tenant(app, "営業", ["owner@example.com"])
    with app.state.sessions.begin() as db:
        db.get(Project, pid).tenant_id = sales
    login(client, "owner@example.com")
    rows = client.get("/api/projects").json()
    assert [row["tenant_id"] for row in rows] == [sales]
    # 名前は別に引く。一覧に埋めると、テナント名を直したとき古いまま残る。
    assert {t["id"]: t["name"] for t in client.get("/api/tenants").json()}[sales] == "営業"


def test_every_project_has_a_tenant(context):
    client, app, (pid, _, _, _) = context
    login(client, "owner@example.com")
    rows = client.get("/api/projects").json()
    assert [row["id"] for row in rows] == [pid]
    assert rows[0]["tenant_id"] is not None
