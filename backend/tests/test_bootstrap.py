"""初期管理者の登録。新規環境でも既定テナントに属した状態で始まる。"""
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from backend.bootstrap import DEFAULT_TENANT_ID, ensure_admin
from backend.core.db import Base, Tenant, User, UserTenant


def sessions(tmp_path, default_tenant=True):
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    made = sessionmaker(engine)
    if default_tenant:
        with made.begin() as db:
            db.add(Tenant(id=DEFAULT_TENANT_ID, name="既定テナント"))
    return made


def memberships(made, email):
    with made() as db:
        user = db.scalar(select(User).where(User.email == email))
        return user.role, list(db.scalars(select(UserTenant.tenant_id).where(UserTenant.user_id == user.id)))


def test_a_new_admin_joins_the_default_tenant(tmp_path):
    made = sessions(tmp_path)
    with made.begin() as db:
        ensure_admin(db, "admin@example.com")
    assert memberships(made, "admin@example.com") == ("admin", [DEFAULT_TENANT_ID])


def test_rerunning_does_not_duplicate_the_membership(tmp_path):
    made = sessions(tmp_path)
    for _ in range(2):
        with made.begin() as db:
            ensure_admin(db, "admin@example.com")
    assert memberships(made, "admin@example.com")[1] == [DEFAULT_TENANT_ID]


def test_an_admin_created_without_a_tenant_is_repaired(tmp_path):
    made = sessions(tmp_path)
    with made.begin() as db:
        db.add(User(email="admin@example.com", role="admin"))
    with made.begin() as db:
        ensure_admin(db, "admin@example.com")
    assert memberships(made, "admin@example.com")[1] == [DEFAULT_TENANT_ID]


def test_a_membership_chosen_by_an_administrator_is_kept(tmp_path):
    made = sessions(tmp_path)
    with made.begin() as db:
        other = Tenant(name="別テナント")
        admin = User(email="admin@example.com", role="admin")
        db.add_all([other, admin]); db.flush()
        db.add(UserTenant(user_id=admin.id, tenant_id=other.id))
        other_id = other.id
    with made.begin() as db:
        ensure_admin(db, "admin@example.com")
    assert memberships(made, "admin@example.com")[1] == [other_id]


def test_without_the_default_tenant_nothing_is_assigned(tmp_path):
    made = sessions(tmp_path, default_tenant=False)
    with made.begin() as db:
        ensure_admin(db, "admin@example.com")
    assert memberships(made, "admin@example.com") == ("admin", [])
