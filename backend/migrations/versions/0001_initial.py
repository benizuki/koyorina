"""Initial schema for the re-released application.

This is the squashed schema that previously lived in revisions 0001 through
0018. The re-release starts with a new database, so historical upgrade steps
and their data migrations are intentionally not carried forward.
"""
from alembic import op
import sqlalchemy as sa


revision = "0001"
down_revision = None

DEFAULT_TENANT_ID = "00000000-0000-4000-8000-000000000001"


def upgrade():
    op.create_table(
        "departments",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(80), nullable=False, unique=True),
        sa.Column("note", sa.String(200), nullable=False, server_default=""),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "users",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("email", sa.String(320), nullable=False, unique=True),
        sa.Column("display_name", sa.String(80), nullable=False, server_default=""),
        sa.Column("google_subject", sa.String(64), nullable=True),
        sa.Column("department_id", sa.String(36), sa.ForeignKey("departments.id"), nullable=True),
        sa.Column("role", sa.String(20), nullable=False, server_default="user"),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("codex_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_users_department_id", "users", ["department_id"])

    op.create_table(
        "tenants",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(80), nullable=False, unique=True),
        sa.Column("note", sa.String(200), nullable=False, server_default=""),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_table(
        "projects",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("purpose", sa.Text, nullable=False),
        sa.Column("audience", sa.String(30), nullable=False),
        sa.Column("fields", sa.JSON, nullable=False),
        sa.Column("tables", sa.JSON, nullable=True),
        sa.Column("requirements", sa.JSON, nullable=True),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("revision", sa.Integer, nullable=False),
        sa.Column("approved_revision", sa.Integer, nullable=True),
        sa.Column("preview_env", sa.JSON, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_projects_owner_id", "projects", ["owner_id"])
    op.create_index("ix_projects_tenant", "projects", ["tenant_id"])

    op.create_table(
        "user_tenants",
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_user_tenants_tenant", "user_tenants", ["tenant_id"])

    op.create_table(
        "generation_jobs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("owner_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("chat_id", sa.String(36), nullable=False),
        sa.Column("revision", sa.Integer, nullable=False),
        sa.Column("specification", sa.JSON, nullable=False),
        sa.Column("instruction", sa.Text, nullable=True),
        sa.Column("attachments", sa.JSON, nullable=True),
        sa.Column("source_type", sa.String(30), nullable=False, server_default="managed_codex"),
        sa.Column("artifact", sa.JSON, nullable=True),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("error", sa.Text, nullable=True),
        sa.Column("provider", sa.String(20), nullable=True),
        sa.Column("model", sa.String(80), nullable=True),
        sa.Column("effort", sa.String(20), nullable=True),
        sa.Column("input_tokens", sa.Integer, nullable=True),
        sa.Column("output_tokens", sa.Integer, nullable=True),
        sa.Column("cached_tokens", sa.Integer, nullable=True),
        sa.Column("total_tokens", sa.Integer, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_generation_jobs_project_id", "generation_jobs", ["project_id"])
    op.create_index("ix_generation_jobs_owner_id", "generation_jobs", ["owner_id"])
    op.create_index("ix_generation_jobs_chat_id", "generation_jobs", ["chat_id"])

    op.create_table(
        "project_collaborators",
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("added_by", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_project_collaborators_user", "project_collaborators", ["user_id"])

    op.create_table(
        "project_sessions",
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "audit_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("actor_id", sa.String(36), nullable=True),
        sa.Column("action", sa.String(80), nullable=False),
        sa.Column("resource_id", sa.String(36), nullable=True),
        sa.Column("detail", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "support_sessions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("actor_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("permission", sa.String(10), nullable=False),
        sa.Column("reason", sa.String(500), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_support_actor", "support_sessions", ["actor_id"])
    op.create_index("ix_support_tenant", "support_sessions", ["tenant_id"])
    op.create_index("ix_support_expires", "support_sessions", ["expires_at"])

    op.create_table(
        "tenant_migrations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source_tenant_id", sa.String(36), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("target_tenant_id", sa.String(36), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("actor_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("reason", sa.String(500), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("error", sa.Text, nullable=True),
        sa.Column("source_retained_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    for name, columns in (
        ("ix_migration_project", ["project_id"]),
        ("ix_migration_source", ["source_tenant_id"]),
        ("ix_migration_target", ["target_tenant_id"]),
        ("ix_migration_status", ["status"]),
    ):
        op.create_index(name, "tenant_migrations", columns)

    op.create_table(
        "storage_usage_snapshots",
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("kind", sa.String(20), primary_key=True),
        sa.Column("claim_name", sa.String(253), nullable=False),
        sa.Column("requested_bytes", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("used_bytes", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("capacity_bytes", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("available_bytes", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(20), nullable=False, server_default="not_measured"),
        sa.Column("error", sa.String(300), nullable=True),
        sa.Column("measured_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.execute(
        sa.text("INSERT INTO tenants (id, name) VALUES (:id, :name)").bindparams(
            id=DEFAULT_TENANT_ID, name="既定テナント"
        )
    )


def downgrade():
    raise RuntimeError("初期スキーマを削除するため自動downgradeは禁止です。")
