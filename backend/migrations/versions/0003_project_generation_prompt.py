"""Store the user-editable request sent to the generation AI."""
from alembic import op
import sqlalchemy as sa


revision = "0003"
down_revision = "0002"


def upgrade():
    op.add_column("projects", sa.Column("generation_prompt", sa.Text(), nullable=True))


def downgrade():
    op.drop_column("projects", "generation_prompt")
