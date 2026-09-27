"""Store the guided creation choices and confirmed sample-data mapping."""
from alembic import op
import sqlalchemy as sa


revision = "0002"
down_revision = "0001"


def upgrade():
    op.add_column("projects", sa.Column("creation_profile", sa.JSON(), nullable=True))


def downgrade():
    op.drop_column("projects", "creation_profile")
