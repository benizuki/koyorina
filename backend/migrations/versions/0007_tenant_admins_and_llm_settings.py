"""Tenant administrators, and generation AI settings per tenant."""
from alembic import op
import sqlalchemy as sa


revision = "0007"
down_revision = "0006"


def upgrade():
    op.add_column("user_tenants", sa.Column("is_admin", sa.Boolean(), nullable=False,
                                            server_default=sa.false()))
    op.create_table(
        "tenant_llm_settings",
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id", ondelete="CASCADE"),
                  primary_key=True),
        sa.Column("kind", sa.String(40), primary_key=True),
        sa.Column("value", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade():
    op.drop_table("tenant_llm_settings")
    op.drop_column("user_tenants", "is_admin")
