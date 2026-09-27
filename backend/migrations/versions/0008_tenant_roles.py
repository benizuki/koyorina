"""Per-tenant roles.

システムロールは admin（全テナントを管理）と member（テナントごとのロールに従う）の2つにし、
アプリを作れるか・テナントを管理できるかは、テナントごとのロール（user_tenants.role）で決める。
これまでの値は、同じ権限になるように移す:

- テナント管理者（is_admin）だった所属 → admin
- それ以外の所属 → 利用者のこれまでの全体ロール（developer / user）。システム管理者は admin
- 全体ロール developer / user → member
"""
from alembic import op
import sqlalchemy as sa


revision = "0008"
down_revision = "0007"


def upgrade():
    op.add_column("user_tenants", sa.Column("role", sa.String(20), nullable=False,
                                            server_default="user"))
    op.execute("""
        UPDATE user_tenants SET role = CASE
            WHEN is_admin THEN 'admin'
            ELSE COALESCE((SELECT CASE users.role WHEN 'admin' THEN 'admin'
                                                 WHEN 'developer' THEN 'developer'
                                                 ELSE 'user' END
                           FROM users WHERE users.id = user_tenants.user_id), 'user')
        END
    """)
    op.execute("UPDATE users SET role = 'member' WHERE role <> 'admin'")
    op.drop_column("user_tenants", "is_admin")


def downgrade():
    op.add_column("user_tenants", sa.Column("is_admin", sa.Boolean(), nullable=False,
                                            server_default=sa.false()))
    op.execute("UPDATE user_tenants SET is_admin = (role = 'admin')")
    # 全体ロールはテナントごとの中で一番強いものへ戻す（テナントごとの差は失われる）。
    op.execute("""
        UPDATE users SET role = CASE
            WHEN EXISTS (SELECT 1 FROM user_tenants WHERE user_tenants.user_id = users.id
                         AND user_tenants.role IN ('admin', 'developer')) THEN 'developer'
            ELSE 'user' END
        WHERE role = 'member'
    """)
    op.drop_column("user_tenants", "role")
