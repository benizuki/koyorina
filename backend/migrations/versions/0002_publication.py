"""Publication records, resources and independent tenant roles."""
from alembic import op
import sqlalchemy as sa
revision = '0002'
down_revision = '0001'


def upgrade():
    op.create_table('app_builds',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('project_id', sa.String(36), sa.ForeignKey('projects.id'), nullable=False),
        sa.Column('tenant_id', sa.String(36), sa.ForeignKey('tenants.id'), nullable=False),
        sa.Column('generation_id', sa.String(36), nullable=False),
        sa.Column('revision', sa.Integer, nullable=False),
        sa.Column('actor_id', sa.String(36), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('source_hash', sa.String(64), nullable=False),
        sa.Column('registry_kind', sa.String(20), nullable=False),
        sa.Column('image', sa.String(500), nullable=False),
        sa.Column('digest', sa.String(80), nullable=True),
        sa.Column('status', sa.String(20), nullable=False),
        sa.Column('error', sa.String(300), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False))
    op.create_index('ix_app_builds_project_id', 'app_builds', ['project_id'])
    op.create_table('app_publications',
        sa.Column('project_id', sa.String(36), sa.ForeignKey('projects.id'), primary_key=True),
        sa.Column('build_id', sa.String(36), sa.ForeignKey('app_builds.id'), nullable=True),
        sa.Column('status', sa.String(20), nullable=False),
        sa.Column('error', sa.String(300), nullable=True),
        sa.Column('environment', sa.JSON, nullable=False),
        sa.Column('resources', sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False))
    op.create_table('publication_grants',
        sa.Column('project_id', sa.String(36), sa.ForeignKey('projects.id'), primary_key=True),
        sa.Column('kind', sa.String(20), primary_key=True),
        sa.Column('subject_id', sa.String(36), primary_key=True))
    op.create_table('publication_events',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('project_id', sa.String(36), sa.ForeignKey('projects.id'), nullable=False),
        sa.Column('build_id', sa.String(36), nullable=True),
        sa.Column('actor_id', sa.String(36), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('action', sa.String(30), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False))
    op.create_index('ix_publication_events_project_id', 'publication_events', ['project_id'])

    op.create_table('user_tenant_roles',
        sa.Column('user_id', sa.String(36), sa.ForeignKey('users.id', ondelete='CASCADE'), primary_key=True),
        sa.Column('tenant_id', sa.String(36), sa.ForeignKey('tenants.id', ondelete='CASCADE'), primary_key=True),
        sa.Column('role', sa.String(20), primary_key=True))
    table = sa.table('user_tenant_roles', sa.column('user_id'), sa.column('tenant_id'), sa.column('role'))
    connection = op.get_bind()
    for user_id, tenant_id, role in connection.execute(sa.text(
            'SELECT user_id, tenant_id, role FROM user_tenants')):
        # The old roles were hierarchical. Preserve every existing capability.
        roles = {'admin': ('admin', 'developer', 'operator', 'user'),
                 'developer': ('developer', 'operator', 'user'), 'user': ('user',)}.get(role, (role,))
        connection.execute(table.insert(), [
            {'user_id': user_id, 'tenant_id': tenant_id, 'role': item} for item in roles])


def downgrade():
    op.drop_table('user_tenant_roles')
    for name in ('publication_events', 'publication_grants', 'app_publications', 'app_builds'):
        op.drop_table(name)
