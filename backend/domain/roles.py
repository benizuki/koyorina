"""System role plus independent tenant roles.

Tenant administrator, developer, operator and user are separately assignable.
The legacy single-role column remains as a compatibility summary; authorization
uses user_tenant_roles after migration.
"""
SYSTEM_ROLES = {
    "admin": "全テナントを管理",
    "member": "テナントごとのロールに従う",
}
TENANT_ROLES = {
    "admin": "テナントの設定を管理",
    "developer": "アプリを開発・ビルド",
    "operator": "公開アプリを運用し、利用者を設定",
    "user": "許可された公開アプリを利用",
}
DEFAULT_ROLE = "member"
DEFAULT_TENANT_ROLE = "user"
DEVELOPER_ROLES = frozenset({"developer"})
LEGACY_ROLE_SETS = {"admin": frozenset({"admin", "developer", "operator", "user"}),
                    "developer": frozenset({"developer", "operator", "user"}),
                    "user": frozenset({"user"})}
# テナントの区分ができる前の利用者（テストやオフラインの取り込み）。全体ロールだけを持つ。
LEGACY_DEFAULT_TENANT = "00000000-0000-4000-8000-000000000001"


def can_manage(user) -> bool:
    """利用者・組織・テナントのマスターを触れるのはシステム管理者だけ。"""
    return getattr(user, "role", DEFAULT_ROLE) == "admin"


def tenant_roles(db, user) -> dict[str, str]:
    """Compatibility summary for clients that show one principal role."""
    from sqlalchemy import select
    from backend.core.db import Tenant, UserTenant
    if can_manage(user):
        return {tenant_id: "admin" for tenant_id in db.scalars(select(Tenant.id))}
    memberships = list(db.scalars(select(UserTenant).where(UserTenant.user_id == str(user.id))))
    return {m.tenant_id: next((role for role in TENANT_ROLES if role in tenant_role_set(db, user, m.tenant_id)),
                               m.role) for m in memberships}


def tenant_role_set(db, user, tenant_id) -> frozenset[str]:
    """Current independent roles; old rows still work before migration in local tests."""
    from sqlalchemy import select
    from backend.core.db import Tenant, UserTenant, UserTenantRole
    if can_manage(user):
        return frozenset(TENANT_ROLES)
    membership = db.get(UserTenant, (str(user.id), str(tenant_id)))
    if membership is not None:
        assigned = frozenset(db.scalars(select(UserTenantRole.role).where(
            UserTenantRole.user_id == str(user.id), UserTenantRole.tenant_id == str(tenant_id))))
        return assigned or LEGACY_ROLE_SETS.get(membership.role, frozenset({membership.role}))
    if str(tenant_id) == LEGACY_DEFAULT_TENANT and db.get(Tenant, LEGACY_DEFAULT_TENANT) is None:
        legacy = getattr(user, 'role', DEFAULT_ROLE)
        return LEGACY_ROLE_SETS.get(legacy, frozenset())
    return frozenset()


def tenant_role_sets(db, user) -> dict[str, list[str]]:
    from sqlalchemy import select
    from backend.core.db import Tenant, UserTenant
    ids = db.scalars(select(Tenant.id)) if can_manage(user) else db.scalars(
        select(UserTenant.tenant_id).where(UserTenant.user_id == str(user.id)))
    return {tenant_id: sorted(tenant_role_set(db, user, tenant_id)) for tenant_id in ids}


def tenant_role(db, user, tenant_id) -> str | None:
    roles = tenant_role_set(db, user, tenant_id)
    return next((role for role in TENANT_ROLES if role in roles), None)


def can_develop_in(db, user, tenant_id) -> bool:
    """そのテナントでアプリを作れるか（仕様・生成・共同開発・オーナー）。"""
    return "developer" in tenant_role_set(db, user, tenant_id)


def developer_tenant_ids(db, user) -> set[str]:
    """アプリを作れるテナント。"""
    return {tenant_id for tenant_id, roles in tenant_role_sets(db, user).items() if 'developer' in roles}


def can_develop_somewhere(db, user) -> bool:
    """どこかのテナントでアプリを作れるか。テナントを決める前の操作（目的の下書き等）に使う。"""
    if developer_tenant_ids(db, user):
        return True
    return can_develop_in(db, user, LEGACY_DEFAULT_TENANT)


def admin_tenant_ids(db, user) -> set[str]:
    """管理できるテナント。システム管理者は全部、テナント管理者は自分が管理者のテナントだけ。"""
    return {tenant_id for tenant_id, roles in tenant_role_sets(db, user).items() if 'admin' in roles}


def operator_tenant_ids(db, user) -> set[str]:
    return {tenant_id for tenant_id, roles in tenant_role_sets(db, user).items() if 'operator' in roles}


def can_operate_tenant(db, user, tenant_id) -> bool:
    return 'operator' in tenant_role_set(db, user, tenant_id)


def can_manage_tenant(db, user, tenant_id) -> bool:
    """テナントの生成AIの設定と利用状況を扱えるか。"""
    return can_manage(user) or str(tenant_id) in admin_tenant_ids(db, user)
