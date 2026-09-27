"""ロール。システム全体で1つと、テナントごとに1つ。

細かい権限を並べると、運用する人が選べなくなる。役割の単位で分ける。

- システムロール: admin（全テナントを管理）か member（テナントごとのロールに従う）。
- テナントロール: そのテナントで何ができるか。admin はテナント管理者（生成AIの接続先・
  キーと利用状況を扱える。アプリも作れる）、developer はアプリを作る、user は使うだけ。
  同じ人が、テナントAでは開発者、テナントBでは利用者、ということがあり得る。
"""
SYSTEM_ROLES = {
    "admin": "全テナントを管理",
    "member": "テナントごとのロールに従う",
}
TENANT_ROLES = {
    "admin": "テナント管理者（設定・利用状況を管理し、アプリも作れる）",
    "developer": "アプリを開発",
    "user": "アプリを利用",
}
DEFAULT_ROLE = "member"
DEFAULT_TENANT_ROLE = "user"
DEVELOPER_ROLES = frozenset({"admin", "developer"})
# テナントの区分ができる前の利用者（テストやオフラインの取り込み）。全体ロールだけを持つ。
LEGACY_DEFAULT_TENANT = "00000000-0000-4000-8000-000000000001"


def can_manage(user) -> bool:
    """利用者・組織・テナントのマスターを触れるのはシステム管理者だけ。"""
    return getattr(user, "role", DEFAULT_ROLE) == "admin"


def tenant_roles(db, user) -> dict[str, str]:
    """{テナントID: テナントロール}。システム管理者は全テナントで admin。"""
    from sqlalchemy import select
    from backend.core.db import Tenant, UserTenant
    if can_manage(user):
        return {tenant_id: "admin" for tenant_id in db.scalars(select(Tenant.id))}
    return dict(db.execute(select(UserTenant.tenant_id, UserTenant.role)
                           .where(UserTenant.user_id == str(user.id))).all())


def tenant_role(db, user, tenant_id) -> str | None:
    """そのテナントでのロール。属していなければ None。"""
    from backend.core.db import Tenant, UserTenant
    if can_manage(user):
        return "admin"
    membership = db.get(UserTenant, (str(user.id), str(tenant_id)))
    if membership is not None:
        return membership.role
    if str(tenant_id) == LEGACY_DEFAULT_TENANT and db.get(Tenant, LEGACY_DEFAULT_TENANT) is None:
        legacy = getattr(user, "role", DEFAULT_ROLE)
        return legacy if legacy in TENANT_ROLES else None
    return None


def can_develop_in(db, user, tenant_id) -> bool:
    """そのテナントでアプリを作れるか（仕様・生成・共同開発・オーナー）。"""
    return tenant_role(db, user, tenant_id) in DEVELOPER_ROLES


def developer_tenant_ids(db, user) -> set[str]:
    """アプリを作れるテナント。"""
    return {tenant_id for tenant_id, role in tenant_roles(db, user).items() if role in DEVELOPER_ROLES}


def can_develop_somewhere(db, user) -> bool:
    """どこかのテナントでアプリを作れるか。テナントを決める前の操作（目的の下書き等）に使う。"""
    if developer_tenant_ids(db, user):
        return True
    return can_develop_in(db, user, LEGACY_DEFAULT_TENANT)


def admin_tenant_ids(db, user) -> set[str]:
    """管理できるテナント。システム管理者は全部、テナント管理者は自分が管理者のテナントだけ。"""
    return {tenant_id for tenant_id, role in tenant_roles(db, user).items() if role == "admin"}


def can_manage_tenant(db, user, tenant_id) -> bool:
    """テナントの生成AIの設定と利用状況を扱えるか。"""
    return can_manage(user) or str(tenant_id) in admin_tenant_ids(db, user)
