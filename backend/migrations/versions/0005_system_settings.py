"""System-wide settings edited from the administration screen."""
from alembic import op
import sqlalchemy as sa


revision = "0005"
down_revision = "0004"


def upgrade():
    op.create_table(
        "system_settings",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("value", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )


def downgrade():
    op.drop_table("system_settings")
