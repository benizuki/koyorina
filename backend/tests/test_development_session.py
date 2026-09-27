"""開発セッション。2人が同時に同じアプリを触らないようにする。

正しさ自体は既存の仕組みが担保している（生成はPodのロック、仕様は revision の
突き合わせ）。ここで確かめるのは「作業してから弾かれる」のではなく、
入った時点で分かることと、席が自動で空くこと。
"""
from datetime import datetime, timedelta, timezone
import pytest
from backend.core.db import ProjectSession, User
from backend.tests.test_collaborators import context, login, share  # noqa: F401

pytestmark = pytest.mark.usefixtures("context")


def enter(client, pid):
    return client.post(f"/api/projects/{pid}/session", json={})


def test_the_second_person_can_look_but_not_change(context):
    client, app, (pid, jid, owner_id, mate_id) = context
    share(app, pid, mate_id, owner_id)
    login(client, "owner@example.com")
    held = enter(client, pid).json()
    assert held["mine"] is True and held["editable"] is True

    login(client, "mate@example.com")
    view = enter(client, pid).json()
    assert view["mine"] is False and view["editable"] is False
    assert view["holder_name"] == "Owner"
    # オーナーではないので引き取れない。
    assert view["can_take_over"] is False
    # 見るほうは通る。
    assert client.get(f"/api/projects/{pid}/jobs").json()[0]["id"] == jid
    assert client.get(f"/api/projects/{pid}/session").json()["holder_name"] == "Owner"
    # 書き換えは、作業する前に止める。
    refused = client.post(f"/api/projects/{pid}/approve", json={"revision": 1})
    assert refused.status_code == 409
    assert "Owner さんがこのアプリを開発中です" in refused.json()["error"]


def test_nobody_holding_it_means_everybody_may_edit(context):
    """席を取らないまま編集できなくなると、セッションの不具合で全員が締め出される。"""
    client, app, (pid, _, owner_id, mate_id) = context
    share(app, pid, mate_id, owner_id)
    login(client, "mate@example.com")
    assert client.post(f"/api/projects/{pid}/approve", json={"revision": 1}).status_code == 200


def test_leaving_frees_the_seat(context):
    client, app, (pid, _, owner_id, mate_id) = context
    share(app, pid, mate_id, owner_id)
    login(client, "owner@example.com")
    enter(client, pid)
    assert client.request("DELETE", f"/api/projects/{pid}/session",
                          json={}).json()["holder_id"] is None
    login(client, "mate@example.com")
    assert enter(client, pid).json()["mine"] is True


def test_a_seat_nobody_returns_to_expires(context):
    """ブラウザを閉じたまま帰った人を待ち続けない。"""
    client, app, (pid, _, owner_id, mate_id) = context
    share(app, pid, mate_id, owner_id)
    login(client, "owner@example.com")
    enter(client, pid)
    with app.state.sessions.begin() as db:
        db.get(ProjectSession, pid).expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    login(client, "mate@example.com")
    assert enter(client, pid).json()["mine"] is True
    assert client.post(f"/api/projects/{pid}/approve", json={"revision": 1}).status_code == 200


def test_only_the_owner_can_pull_the_seat_back(context):
    """作業中の人を誰でも追い出せてはいけない。戻らない人を片付ける手段だけを残す。"""
    client, app, (pid, _, owner_id, mate_id) = context
    share(app, pid, mate_id, owner_id)
    login(client, "mate@example.com")
    enter(client, pid)

    login(client, "owner@example.com")
    view = client.get(f"/api/projects/{pid}/session").json()
    assert view["editable"] is False and view["can_take_over"] is True
    assert client.post(f"/api/projects/{pid}/session?takeover=true", json={}).json()["mine"] is True

    # 共同開発者には引き取りを許さない。
    login(client, "mate@example.com")
    taken = client.post(f"/api/projects/{pid}/session?takeover=true", json={}).json()
    assert taken["mine"] is False and taken["holder_name"] == "Owner"


def test_a_collaborator_generates_with_their_own_codex_account(context, monkeypatch):
    """Codexは依頼した人自身の枠で動く。オーナーの枠は削られない。"""
    client, app, (pid, _, owner_id, mate_id) = context
    share(app, pid, mate_id, owner_id)
    calls = []

    async def controller(settings, user_id, method, path, *args, **kwargs):
        calls.append((user_id, path))
        return {"status": "connected", "generator": "codex"} if path == "/account" else {"status": "starting"}

    monkeypatch.setattr("backend.api.generation.controller", controller)
    with app.state.sessions.begin() as db:
        db.get(User, mate_id).codex_enabled = True
    login(client, "mate@example.com")
    assert client.post(f"/api/projects/{pid}/generate",
                       json={"provider": "codex"}).status_code == 202
    # 問い合わせ先は共同開発者自身のPod。オーナーのPodは一度も呼ばない。
    assert {user for user, _ in calls} == {mate_id}
