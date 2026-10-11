"""Departments become a hierarchy (division / section / team)."""
from alembic import op
import sqlalchemy as sa
revision = '0003'
down_revision = '0002'


def name_constraints(bind):
    # 0001 は名前を付けずに unique=True と書いた。PostgreSQL では departments_name_key になるが、
    # 決め打ちせず、name だけを対象にした一意制約を探して外す。
    return [item['name'] for item in sa.inspect(bind).get_unique_constraints('departments')
            if item['column_names'] == ['name'] and item['name']]


def upgrade():
    bind = op.get_bind()
    with op.batch_alter_table('departments') as batch:
        for name in name_constraints(bind):
            batch.drop_constraint(name, type_='unique')
        batch.add_column(sa.Column('parent_id', sa.String(36), nullable=True))
        batch.add_column(sa.Column('kind', sa.String(20), nullable=False, server_default='division'))
        batch.add_column(sa.Column('sort_order', sa.Integer, nullable=False, server_default='0'))
        batch.create_foreign_key('fk_departments_parent_id', 'departments', ['parent_id'], ['id'])
        batch.create_index('ix_departments_parent_id', ['parent_id'])


def downgrade():
    with op.batch_alter_table('departments') as batch:
        batch.drop_index('ix_departments_parent_id')
        batch.drop_constraint('fk_departments_parent_id', type_='foreignkey')
        batch.drop_column('sort_order')
        batch.drop_column('kind')
        batch.drop_column('parent_id')
        batch.create_unique_constraint('departments_name_key', ['name'])
