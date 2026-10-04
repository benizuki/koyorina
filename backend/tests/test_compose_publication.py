"""Compose publication keeps durable state when Docker operations fail."""
import asyncio
from uuid import uuid4

import pytest
from fastapi import HTTPException

from backend.config.settings import Settings
from backend.worker import compose_publication as publication


def test_compose_accepts_loopback_port_and_fixed_controller():
    settings = Settings(database_url='postgresql+psycopg://u:p@localhost/db',
        app_env='local', app_origin='http://localhost:8081', publication_enabled=True,
        publication_controller_url='http://publication-controller:8080',
        publication_controller_token='x' * 40)
    assert settings.app_origin == 'http://localhost:8081'
    with pytest.raises(ValueError):
        Settings(database_url='postgresql+psycopg://u:p@localhost/db',
            app_env='local', app_origin='http://0.0.0.0:8081')


def test_interrupted_build_is_failed_after_controller_restart(tmp_path, monkeypatch):
    monkeypatch.setattr(publication, 'ROOT', tmp_path)
    identifier = uuid4()
    publication.write('build', identifier, {'id': str(identifier), 'status': 'building'})
    asyncio.run(publication.ComposeController().startup())
    state = publication.read('build', identifier)
    assert state['status'] == 'failed' and '再実行' in state['error']


def test_failed_volume_removal_preserves_publication_record(tmp_path, monkeypatch):
    monkeypatch.setattr(publication, 'ROOT', tmp_path)
    identifier = uuid4()
    publication.write('published', identifier, {'project_id': str(identifier), 'status': 'stopped'})

    async def failing_docker(*args, **kwargs):
        if args[0] == 'inspect':
            return 1, 'not found'
        if args[:2] == ('volume', 'inspect'):
            return 0, '{}'
        if args[:2] == ('volume', 'rm'):
            return 1, 'volume is in use'
        raise AssertionError(args)

    monkeypatch.setattr(publication, 'docker', failing_docker)
    with pytest.raises(HTTPException) as error:
        asyncio.run(publication.ComposeController().purge(identifier))
    assert error.value.status_code == 503
    assert publication.read('published', identifier)['status'] == 'stopped'
