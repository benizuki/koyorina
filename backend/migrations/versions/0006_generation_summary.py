"""Keep the generation AI's final report and the features it deferred, for the chat."""
from alembic import op
import sqlalchemy as sa


revision = "0006"
down_revision = "0005"


def upgrade():
    op.add_column("generation_jobs", sa.Column("summary", sa.Text(), nullable=True))
    op.add_column("generation_jobs", sa.Column("next_steps", sa.JSON(), nullable=True))


def downgrade():
    op.drop_column("generation_jobs", "next_steps")
    op.drop_column("generation_jobs", "summary")
