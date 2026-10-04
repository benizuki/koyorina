"""共同開発者。削除と共有設定だけをオーナー・管理者に残す。

分け方を間違えると、共有した相手にアプリごと消される。元へ戻す手立てが無いので、
「触れる」と「消せる」を別の関門にしてある。
"""
import pytest
from fastapi.testclient import TestClient
from backend.config.settings import Settings
from backend.core.db import Base, GenerationJob, Project, ProjectCollaborator, Tenant, User, UserTenant
from backend.main import create_app


@pytest.fixture
def context(tmp_path, monkeypatch):
    settings = Settings(_env_file=None, app_env="production",
        database_url="postgresql+psycopg://test:test@localhost/test", app_origin="https://forge.test",
        app_session_secret="test-only-session-secret-at-least-32-characters",
        google_oauth_client_id="test-client")
    settings = settings.model_copy(update={"database_url": f"sqlite:///{tmp_path / 'test.db'}"})
    app = create_app(settings)
    Base.metadata.create_all(app.state.sessions.kw["bind"])
    with app.state.sessions.begin() as db:
        # 全体ロールは member。開発できるかはテナントごとのロールで決まる。
        owner = User(email="owner@example.com", role="member", display_name="Owner")
        mate = User(email="mate@example.com", role="member", display_name="Mate")
        reader = User(email="reader@example.com", role="member")
        db.add_all([owner, mate, reader, User(email="admin@example.com", role="admin"),
                    User(email="outsider@example.com", role="member")]); db.flush()
        tenant = Tenant(name="既定テナント")
        db.add(tenant); db.flush()
        for member in (owner, mate):
            db.add(UserTenant(user_id=member.id, tenant_id=tenant.id, role="developer"))
        # 同じテナントにいても、使うだけの人は共同開発者・オーナーにできない。
        db.add(UserTenant(user_id=reader.id, tenant_id=tenant.id, role="user"))
        # 生成まで通すので、仕様として成り立つ中身を入れておく。
        field = {"name": "備品名", "kind": "text", "required": True}
        project = Project(owner_id=owner.id, tenant_id=tenant.id, name="備品管理", purpose="備品の貸出を管理する",
                          audience="team", fields=[field], status="approved", approved_revision=1,
                          tables=[{"name": "備品", "kind": "record", "fields": [field]}],
                          requirements=[])
        db.add(project); db.flush()
        job = GenerationJob(project_id=project.id, owner_id=owner.id, revision=1,
                            specification={}, instruction="Create draft", status="generated")
        db.add(job); db.flush()
        ids = project.id, job.id, owner.id, mate.id
    monkeypatch.setattr("backend.main.id_token.verify_oauth2_token", lambda token, *args:
        {"email": token, "email_verified": True, "iss": "https://accounts.google.com"})
    with TestClient(app, base_url="https://forge.test", headers={"Origin": "https://forge.test"}) as client:
        yield client, app, ids


def login(client, email):
    assert client.post("/auth/google", json={"credential": email}).status_code == 200


def share(app, project_id, user_id, by):
    with app.state.sessions.begin() as db:
        db.add(ProjectCollaborator(project_id=project_id, user_id=user_id, added_by=by))


def test_a_collaborator_sees_and_edits_but_cannot_delete(context):
    client, app, (pid, jid, owner_id, mate_id) = context
    login(client, "mate@example.com")
    # 共有される前は、存在すら知らせない。
    assert client.get("/api/projects").json() == []
    assert client.get(f"/api/projects/{pid}/jobs").status_code == 404

    share(app, pid, mate_id, owner_id)
    listed = client.get("/api/projects").json()
    assert [item["id"] for item in listed] == [pid]
    assert listed[0]["is_owner"] is False and listed[0]["can_edit"] is True
    assert listed[0]["can_administer"] is False
    assert client.get(f"/api/projects/{pid}/jobs").json()[0]["id"] == jid
    assert client.post(f"/api/projects/{pid}/approve", json={"revision": 1}).status_code == 200
    # 消せるのはオーナーと管理者だけ。
    assert client.request("DELETE", f"/api/projects/{pid}", json={}).status_code == 404
    assert client.get("/api/projects").json() != []


def test_a_collaborator_cannot_invite_anyone_else(context):
    """参加者が別の人を呼べると、オーナーの知らないところで閲覧範囲が広がる。"""
    client, app, (pid, _, owner_id, mate_id) = context
    share(app, pid, mate_id, owner_id)
    login(client, "mate@example.com")
    with app.state.sessions() as db:
        outsider = db.query(User).filter_by(email="outsider@example.com").one().id
    assert client.post(f"/api/projects/{pid}/collaborators",
                       json={"user_id": outsider}).status_code == 404
    # 誰と作業しているかは見える。見えないほうが危うい。
    assert client.get(f"/api/projects/{pid}/collaborators").json()["can_administer"] is False


def test_the_owner_shares_and_takes_it_back(context):
    client, app, (pid, _, _, mate_id) = context
    login(client, "owner@example.com")
    added = client.post(f"/api/projects/{pid}/collaborators", json={"user_id": mate_id})
    assert added.status_code == 201
    assert [item["email"] for item in added.json()["collaborators"]] == ["mate@example.com"]
    # 二度押しても増えない。
    assert len(client.post(f"/api/projects/{pid}/collaborators",
                           json={"user_id": mate_id}).json()["collaborators"]) == 1
    removed = client.request("DELETE", f"/api/projects/{pid}/collaborators/{mate_id}", json={})
    assert removed.status_code == 200 and removed.json()["collaborators"] == []
    login(client, "mate@example.com")
    assert client.get("/api/projects").json() == []


def test_only_developers_can_be_invited(context):
    """使うだけの人を入れると、触れる範囲が役割と食い違う。"""
    client, app, (pid, _, _, _) = context
    login(client, "owner@example.com")
    with app.state.sessions() as db:
        reader = db.query(User).filter_by(email="reader@example.com").one().id
    assert client.post(f"/api/projects/{pid}/collaborators", json={"user_id": reader}).status_code == 409


def test_the_candidate_list_only_offers_people_who_can_be_invited(context):
    """すでに参加している人・オーナー・使うだけの人を出すと、選べない選択肢が並ぶ。"""
    client, app, (pid, _, owner_id, mate_id) = context
    login(client, "owner@example.com")
    offered = client.get(f"/api/projects/{pid}/collaborators/candidates").json()["candidates"]
    assert {item["email"] for item in offered} == {"mate@example.com"}
    client.post(f"/api/projects/{pid}/collaborators", json={"user_id": mate_id})
    offered = client.get(f"/api/projects/{pid}/collaborators/candidates").json()["candidates"]
    assert offered == []
    # 呼べる人だけが候補を見られる。
    login(client, "mate@example.com")
    assert client.get(f"/api/projects/{pid}/collaborators/candidates").status_code == 404


def test_the_owner_can_hand_the_app_over(context):
    """引き取り手のいないアプリが残ると、消すことしかできなくなる。

    作業場所はアプリ単位の共有領域にあるので、持ち主の記録を移すだけで続きから作業できる。
    """
    client, app, (pid, _, owner_id, mate_id) = context
    login(client, "owner@example.com")
    handed = client.post(f"/api/projects/{pid}/owner", json={"user_id": mate_id})
    assert handed.status_code == 200 and handed.json()["owner_id"] == mate_id
    # 元のオーナーは共同開発者として残る。引き継いだ直後に触れなくなると困る。
    assert client.get(f"/api/projects/{pid}/collaborators").json()["collaborators"][0]["email"] \
        == "owner@example.com"
    # 消せるのは新しいオーナー。元のオーナーはもう消せない。
    assert client.request("DELETE", f"/api/projects/{pid}", json={}).status_code == 404
    login(client, "mate@example.com")
    assert client.get("/api/projects").json()[0]["is_owner"] is True


def test_a_collaborator_cannot_hand_the_app_over(context):
    client, app, (pid, _, owner_id, mate_id) = context
    share(app, pid, mate_id, owner_id)
    login(client, "mate@example.com")
    assert client.post(f"/api/projects/{pid}/owner", json={"user_id": mate_id}).status_code == 404


def test_generation_runs_on_the_requester_pod_with_their_own_quota(context, monkeypatch):
    """生成は依頼した人のPodで、その人の枠で動く。

    作業場所はアプリ単位の共有領域にあるので、誰のPodから触っても同じコードの続きになる。
    オーナーのPodで動かすと、共同開発者の依頼でオーナーのChatGPT枠が削られる。
    """
    client, app, (pid, _, owner_id, mate_id) = context
    share(app, pid, mate_id, owner_id)
    calls = []

    async def controller(settings, user_id, method, path, *args, **kwargs):
        calls.append((user_id, method, path))
        if path == "/account":
            return {"status": "connected", "generator": "gemini"}
        return {"status": "starting"}

    monkeypatch.setattr("backend.api.generation.controller", controller)
    login(client, "mate@example.com")
    assert client.post(f"/api/projects/{pid}/generate",
                       json={"provider": "gemini"}).status_code == 202
    assert calls and {user for user, _, _ in calls} == {mate_id}
    with app.state.sessions() as db:
        jobs = db.query(GenerationJob).filter_by(project_id=pid).all()
        assert mate_id in {job.owner_id for job in jobs}


def test_collaborator_change_progress_is_visible_to_both_members(context, monkeypatch):
    client, app, (pid, original_id, owner_id, mate_id) = context
    share(app, pid, mate_id, owner_id)
    calls = []

    async def controller(settings, user_id, method, path, *args, **kwargs):
        calls.append((user_id, method, path))
        if path.endswith("/progress"):
            return {"events": [{"id": 1, "at": "2026-10-02T00:00:00Z",
                                "kind": "status", "message": "Working"}],
                    "last_response_at": None, "response_bytes": 0, "truncated": False}
        if path.endswith("/attachments"):
            return {"attachments": []}
        return {"status": "generating"}

    monkeypatch.setattr("backend.api.generation.controller", controller)
    login(client, "mate@example.com")
    created = client.post(f"/api/projects/{pid}/messages",
                          json={"text": "Fix the preview", "provider": "gemini"})
    assert created.status_code == 202
    job_id = created.json()["id"]

    for email in ("mate@example.com", "owner@example.com"):
        login(client, email)
        listed = client.get(f"/api/projects/{pid}/jobs")
        assert listed.status_code == 200
        assert {item["id"] for item in listed.json()} == {original_id, job_id}
        refreshed = client.post(f"/api/projects/{pid}/jobs/{job_id}/refresh", json={})
        assert refreshed.status_code == 200
        assert refreshed.json()["status"] == "generating"
        progress = client.get(f"/api/projects/{pid}/jobs/{job_id}/progress")
        assert progress.status_code == 200
        assert progress.json()["events"][0]["message"] == "Working"

    job_calls = [call for call in calls if call[2].startswith(f"/jobs/{job_id}")]
    assert job_calls
    assert {user_id for user_id, _, _ in job_calls} == {mate_id}


def test_a_collaborator_is_told_they_may_edit(context):
    """画面はこの可否で開発画面を出す。オーナーかどうかで判定させない。

    実際、画面が owner_id だけを見ていたため、共同開発者は閲覧専用の画面に
    なっていた。呼ばれても何もできない、という状態だった。
    """
    client, app, (pid, _, owner_id, mate_id) = context
    login(client, "mate@example.com")
    share(app, pid, mate_id, owner_id)
    project = next(item for item in client.get("/api/projects").json() if item["id"] == pid)
    assert project["can_edit"] is True and project["is_owner"] is False
    # 共有の設定までは渡さない。呼んだ人が別の人を呼べると、範囲が勝手に広がる。
    assert project["can_administer"] is False


def test_an_administrator_manages_collaborators_and_the_seat(context):
    """オーナーが応じられないときに、運用側で止められること。"""
    client, app, (pid, _, _, mate_id) = context
    login(client, "admin@example.com")
    project = next(item for item in client.get("/api/projects").json() if item["id"] == pid)
    # 中身は書き換えない。呼ぶ・外す・席を引き取るだけ。
    assert project["can_administer"] is True and project.get("can_edit") is False

    added = client.post(f"/api/projects/{pid}/collaborators", json={"user_id": mate_id})
    assert added.status_code == 201
    assert client.request("DELETE", f"/api/projects/{pid}/collaborators/{mate_id}",
                          json={}).status_code == 200

    # 席の引き取り。can_take_over が「できる」と言う以上、APIも受ける。
    login(client, "owner@example.com")
    assert client.post(f"/api/projects/{pid}/session", json={}).json()["mine"] is True
    login(client, "admin@example.com")
    held = client.get(f"/api/projects/{pid}/session").json()
    assert held["can_take_over"] is True
    assert client.post(f"/api/projects/{pid}/session?takeover=true", json={}).json()["mine"] is True


def test_one_project_can_be_read_again_by_the_same_people_as_the_list(context):
    """テナント移行の完了後、画面は1件だけ取り直す。この経路が無く「見つかりません」になっていた。"""
    client, app, (pid, _, owner_id, mate_id) = context
    for email, visible in (("owner@example.com", True), ("admin@example.com", True),
                           ("mate@example.com", False), ("outsider@example.com", False)):
        login(client, email)
        result = client.get(f"/api/projects/{pid}")
        assert result.status_code == (200 if visible else 404), email
        if visible:
            assert result.json()["id"] == pid and "tenant_id" in result.json()
    share(app, pid, mate_id, owner_id)
    login(client, "mate@example.com")
    assert client.get(f"/api/projects/{pid}").status_code == 200
