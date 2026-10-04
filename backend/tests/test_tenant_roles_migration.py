"""Legacy memberships retain their capabilities after the role split."""
import importlib
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


def test_existing_memberships_expand_to_independent_roles():
    migration = importlib.import_module('backend.migrations.versions.0002_publication')
    engine = sa.create_engine('sqlite://')
    with engine.begin() as db:
        db.execute(sa.text('CREATE TABLE projects (id VARCHAR(36) PRIMARY KEY)'))
        db.execute(sa.text('CREATE TABLE users (id VARCHAR(36) PRIMARY KEY)'))
        db.execute(sa.text('CREATE TABLE tenants (id VARCHAR(36) PRIMARY KEY)'))
        db.execute(sa.text('CREATE TABLE user_tenants (user_id VARCHAR(36), tenant_id VARCHAR(36), role VARCHAR(20))'))
        db.execute(sa.text("INSERT INTO user_tenants VALUES ('a', 't', 'admin'), ('d', 't', 'developer'), ('u', 't', 'user')"))
        migration.op = Operations(MigrationContext.configure(db))
        migration.upgrade()
        inspector = sa.inspect(db)
        resources = next(column for column in inspector.get_columns('app_publications')
                         if column['name'] == 'resources')
        assert resources['nullable'] is False
        assert resources['default'] == "'{}'"
        rows = db.execute(sa.text('SELECT user_id, role FROM user_tenant_roles ORDER BY user_id, role')).all()
        migration.downgrade()
        assert set(sa.inspect(db).get_table_names()) == {'projects', 'users', 'tenants', 'user_tenants'}
    engine.dispose()
    assert rows == [('a', 'admin'), ('a', 'developer'), ('a', 'operator'), ('a', 'user'),
                    ('d', 'developer'), ('d', 'operator'), ('d', 'user'), ('u', 'user')]
