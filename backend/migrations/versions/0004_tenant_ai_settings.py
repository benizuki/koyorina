"""Per-tenant Gemini connection defaults for generated applications."""
from alembic import op
import sqlalchemy as sa


revision = "0004"
down_revision = "0003"


def upgrade():
    op.create_table(
        "tenant_ai_settings",
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id", ondelete="CASCADE"),
                  primary_key=True),
        sa.Column("backend", sa.String(20), nullable=False, server_default="none"),
        sa.Column("api_key_encrypted", sa.Text(), nullable=True),
        sa.Column("gcp_project", sa.String(64), nullable=False, server_default=""),
        sa.Column("location", sa.String(64), nullable=False, server_default=""),
        sa.Column("model", sa.String(100), nullable=False, server_default=""),
        sa.Column("thinking_level", sa.String(20), nullable=False, server_default=""),
        sa.Column("wif_project_number", sa.String(20), nullable=False, server_default=""),
        sa.Column("wif_pool_id", sa.String(64), nullable=False, server_default=""),
        sa.Column("wif_provider_id", sa.String(64), nullable=False, server_default=""),
        sa.Column("wif_service_account", sa.String(200), nullable=False, server_default=""),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )


def downgrade():
    op.drop_table("tenant_ai_settings")
