"""Publication invariants, with durable Kubernetes resources simulated across restarts."""
import asyncio
import base64
import copy
import io
import json
import tarfile
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from backend.core.db import (Base, Department, Project, User, Tenant, UserTenant, UserTenantRole,
    AppPublication, PublicationEvent, PublicationGrant, GenerationJob, AppBuild, Audit)
from backend.domain.generation import CodeBundle
from backend.domain.publication import (snapshot, permitted, image_reference,
    version_image_reference, published_secret, PublicationResources)
from backend.tests.test_codex_generation import BUNDLE
from backend.worker.publication_controller import (Controller, ControllerSettings, BuildInput,
    PublishInput, TenantMoveInput, unpack_source, create_controller)

PROJECT, TENANT, BUILD = [str(uuid4()) for _ in range(3)]
SHA = 'sha256:' + 'a' * 64


def settings(**kwargs):
    return ControllerSettings(token='x' * 40, registry_host='registry.example.test:30500',
        registry_secret='push', buildkit_image='buildkit@' + SHA, helper_image='python@' + SHA,
        assets=Path('setup/publication'), **kwargs)


class FakeController(Controller):
    def __init__(self, store=None, **kwargs):
        super().__init__(settings(**kwargs))
        self.store = store if store is not None else {}
        self.deleted = []

    async def kube(self, method, resource, name='', body=None, group='api/v1', runtime=False,
                   query='', text=False, cluster=False):
        key = (runtime, resource, name)
        if method == 'GET':
            if resource == 'daemonsets' and name == 'registry-node':
                return {'status': {'desiredNumberScheduled': 1}}
            if resource == 'pods' and query == '?labelSelector=app%3Dkoyorina-registry-node':
                return {'items': [{'metadata': {}, 'spec': {'nodeName': 'node-1'}}]}
            if resource == 'leases':
                from backend.worker.registry_node import transport_hash
                from datetime import datetime, timezone
                desired = json.loads(self.store[(False, 'configmaps', 'registry-pull-transport')]['data']['transport'])
                return {'items': [{'metadata': {'annotations': {'node': 'node-1',
                    'transport-hash': transport_hash(**desired)}},
                    'spec': {'renewTime': datetime.now(timezone.utc).isoformat()}}]}
            if name:
                return copy.deepcopy(self.store.get(key))
            items = [copy.deepcopy(v) for (r, kind, n), v in self.store.items() if r == runtime and kind == resource]
            if resource == 'configmaps':
                items = [i for i in items if i.get('metadata', {}).get('labels', {}).get('koyorina-record')]
            if resource == 'pods' and query:
                bid = query.split('%3D')[-1]
                label = 'koyorina-published' if runtime else 'koyorina-build'
                items = [i for i in items if i['metadata'].get('labels', {}).get(label) == bid]
            return {'items': items}
        if method in ('POST', 'PATCH'):
            body = copy.deepcopy(body)
            name = name or body['metadata']['name']
            body['metadata'].setdefault('name', name)
            if method == 'PATCH' and resource == 'persistentvolumeclaims' and key in self.store:
                existing = copy.deepcopy(self.store[key])
                existing['spec']['resources']['requests'].update(body['spec']['resources']['requests'])
                body = existing
            self.store[(runtime, resource, name)] = body
            return body
        if method == 'DELETE':
            self.deleted.append(key)
            return self.store.pop(key, None)


@pytest.fixture
def sessions():
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    yield sessionmaker(engine, expire_on_commit=False)
    engine.dispose()


def source():
    raw, sha = snapshot(CodeBundle.model_validate(BUNDLE))
    return BuildInput(tenant_id=TENANT, project_id=PROJECT, revision=2,
        source=base64.b64encode(raw).decode(), source_hash=sha)


def test_snapshot_is_fixed_excludes_data_and_build_files():
    bundle = CodeBundle.model_validate(BUNDLE)
    from backend.domain.generation import SourceFile
    bundle.files.extend([SourceFile(path='backend/data/private.json', content='{"secret":"data"}'),
        SourceFile(path='.env.example', content='TOKEN=secret'),
        SourceFile(path='attachments/test.txt', content='attachment')])
    data, sha = snapshot(bundle)
    with tarfile.open(fileobj=io.BytesIO(data), mode='r:gz') as archive:
        assert 'backend/data/private.json' not in archive.getnames()
        assert '.env.example' not in archive.getnames()
    bundle.files[1].content += '# changed'
    assert snapshot(bundle)[1] != sha
    assert unpack_source(base64.b64encode(data).decode(), sha) == data
    with pytest.raises(HTTPException):
        unpack_source(base64.b64encode(data).decode(), '0' * 64)


def test_malicious_archive_rejected():
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode='w:gz') as archive:
        info = tarfile.TarInfo('../escape'); info.size = 1
        archive.addfile(info, io.BytesIO(b'x'))
    with pytest.raises(HTTPException):
        unpack_source(base64.b64encode(output.getvalue()).decode(), '0' * 64)


def test_image_tags_use_full_uuid_and_secrets_are_distinct():
    assert image_reference('registry:5000', PROJECT, BUILD).endswith(':' + BUILD)
    assert version_image_reference('registry:5000', PROJECT, 2).endswith(':rev2')
    assert version_image_reference('registry:5000', PROJECT, 3) != version_image_reference(
        'registry:5000', PROJECT, 2)
    from backend.domain.preview import forward_secret
    assert published_secret(PROJECT, 'secret') != forward_secret(PROJECT, 'secret')


def test_current_tenant_and_department_membership_required(sessions):
    with sessions.begin() as db:
        tenant = Tenant(id=TENANT, name='team')
        dept = Department(name='sales')
        owner = User(email='owner@example.test', role='developer')
        user = User(email='user@example.test', role='user')
        db.add_all([tenant, dept, owner, user]); db.flush()
        user.department_id = dept.id
        p = Project(id=PROJECT, owner_id=owner.id, tenant_id=TENANT, name='App', purpose='test',
            audience='team', fields=[], tables=[], requirements=[])
        pub = AppPublication(project_id=PROJECT)
        db.add_all([p, pub, PublicationGrant(project_id=PROJECT, kind='department', subject_id=dept.id)])
        db.flush()
        assert not permitted(db, p, user, pub)
        member = UserTenant(user_id=user.id, tenant_id=TENANT)
        db.add(member); db.flush()
        assert permitted(db, p, user, pub)
        dept.enabled = False
        assert not permitted(db, p, user, pub)
        db.add(PublicationGrant(project_id=PROJECT, kind='user', subject_id=user.id)); db.flush()
        assert permitted(db, p, user, pub)
        user.enabled = False
        assert not permitted(db, p, user, pub)
        user.enabled = True
        db.delete(member); db.flush()
        assert not permitted(db, p, user, pub)


def test_build_restarts_without_duplicate_and_cancel_keeps_cache():
    async def run():
        c = FakeController()
        state = await c.submit(BUILD, source())
        assert state['status'] == 'queued'
        await c.reconcile()
        assert (await c.record('build-' + BUILD))['status'] == 'building'
        job = c.store[(False, 'jobs', 'build-' + BUILD)]
        spec = job['spec']['template']['spec']
        assert not spec['automountServiceAccountToken']
        assert not any('hostPath' in v for v in spec['volumes'])
        assert job['spec']['activeDeadlineSeconds'] == 1200
        script = spec['containers'][0]['command'][2]
        assert '\\\\\n' not in script
        assert 'type=image,\\"name=$OUTPUT_NAMES\\"' in script
        assert '--opt image-resolve-mode=pull' in script
        assert '--opt build-arg:SECURITY_UPDATE_ID="$BUILD_ID"' in script
        env = {item['name']: item['value'] for item in spec['containers'][0]['env']}
        assert env['BUILD_ID'] == BUILD
        assert env['OUTPUT_NAMES'] == ','.join((
            image_reference('registry.example.test:30500', PROJECT, BUILD),
            version_image_reference('registry.example.test:30500', PROJECT, 2)))
        restarted = FakeController(c.store)
        await restarted.reconcile()
        assert len([k for k in c.store if k[1] == 'jobs']) == 1
        await restarted.cancel(BUILD)
        assert (await restarted.record('build-' + BUILD))['status'] == 'cancelled'
        assert (False, 'persistentvolumeclaims', 'cache-' + TENANT) in c.store
        old = FakeController()
        old_id = str(uuid4())
        await old.submit(old_id, source().model_copy(update={'revision': None}))
        await old.reconcile()
        old_pod = old.store[(False, 'jobs', 'build-' + old_id)]['spec']['template']['spec']
        old_env = {item['name']: item['value'] for item in old_pod['containers'][0]['env']}
        assert old_env['OUTPUT_NAMES'] == image_reference('registry.example.test:30500', PROJECT, old_id)
    asyncio.run(run())


def test_purge_requires_stopped_podless_publication_and_keeps_build():
    async def run():
        c = FakeController()
        await c.record('build-' + BUILD, {'id': BUILD, 'status': 'succeeded'})
        name = 'published-' + PROJECT
        await c.record(name, {'project_id': PROJECT, 'status': 'running'})
        with pytest.raises(HTTPException) as exc:
            await c.purge(PROJECT)
        assert exc.value.status_code == 409
        await c.record(name, {'project_id': PROJECT, 'status': 'stopped'})
        c.store[(True, 'pods', 'old-pod')] = {'metadata': {
            'labels': {'koyorina-published': name}}}
        with pytest.raises(HTTPException) as exc:
            await c.purge(PROJECT)
        assert exc.value.status_code == 409
        del c.store[(True, 'pods', 'old-pod')]
        c.store[(True, 'persistentvolumeclaims', name + '-data')] = {'metadata': {'name': name + '-data'}}
        c.store[(True, 'secrets', name + '-env-1234')] = {'metadata': {'name': name + '-env-1234'}}
        c.store[(True, 'services', name)] = {'metadata': {'name': name}}
        assert (await c.purge(PROJECT))['status'] == 'deleted'
        assert await c.record(name) is None
        assert (False, 'configmaps', 'build-' + BUILD) in c.store
        assert not any(key[0] and key[1] in ('persistentvolumeclaims', 'services', 'secrets')
            for key in c.store)
        assert (await c.purge(PROJECT))['status'] == 'deleted'
    asyncio.run(run())


def test_publication_storage_is_independent_from_build_cache():
    async def run():
        c = FakeController(data_storage_class='published-rbd', data_access_mode='ReadWriteOncePod')
        await c.claim('cache-' + TENANT, '10Gi')
        await c.claim('published-' + PROJECT + '-data', '5Gi', runtime=True)
        cache = c.store[(False, 'persistentvolumeclaims', 'cache-' + TENANT)]['spec']
        data = c.store[(True, 'persistentvolumeclaims', 'published-' + PROJECT + '-data')]['spec']
        assert cache['storageClassName'] == 'local-path' and cache['accessModes'] == ['ReadWriteOnce']
        assert data['storageClassName'] == 'published-rbd' and data['accessModes'] == ['ReadWriteOncePod']
        c.settings = c.settings.model_copy(update={'data_storage_class': 'new-rbd'})
        await c.claim('published-' + PROJECT + '-data', '5Gi', runtime=True)
        assert c.store[(True, 'persistentvolumeclaims', 'published-' + PROJECT + '-data')]['spec'] == data
    asyncio.run(run())


def test_publication_resources_change_runtime_and_expand_existing_claim():
    async def run():
        c = FakeController(data_storage_class='published-rbd', data_access_mode='ReadWriteOncePod')
        resources = PublicationResources(cpu_request_m=250, cpu_limit_m=750,
            memory_request_mi=512, memory_limit_mi=1536, storage_gi=8)
        claim_name = 'published-' + PROJECT + '-data'
        await c.claim(claim_name, '5Gi', runtime=True)
        c.store[(False, 'storageclasses', 'published-rbd')] = {'allowVolumeExpansion': True}
        await c.claim(claim_name, '8Gi', runtime=True)
        assert c.store[(True, 'persistentvolumeclaims', claim_name)]['spec']['resources']['requests']['storage'] == '8Gi'
        info = await c.resource_info(PROJECT)
        assert info['pvc_size'] == '8Gi' and info['expandable']
        manifest = c.runtime_manifest('published-' + PROJECT, 'image@' + SHA, 'secret', resources)
        assert manifest['spec']['template']['spec']['containers'][0]['resources'] == {
            'requests': {'cpu': '250m', 'memory': '512Mi'},
            'limits': {'cpu': '750m', 'memory': '1536Mi'}}
        with pytest.raises(HTTPException) as exc:
            await c.claim(claim_name, '7Gi', runtime=True)
        assert exc.value.status_code == 422
        c.store[(False, 'storageclasses', 'published-rbd')]['allowVolumeExpansion'] = False
        with pytest.raises(HTTPException) as exc:
            await c.claim(claim_name, '9Gi', runtime=True)
        assert exc.value.status_code == 422
    asyncio.run(run())


def test_publication_resource_limits_must_cover_requests():
    with pytest.raises(ValueError):
        PublicationResources(cpu_request_m=500, cpu_limit_m=400,
            memory_request_mi=512, memory_limit_mi=1024, storage_gi=5)


def test_gce_persistent_disk_requires_ten_gib():
    async def run():
        c = FakeController(data_storage_class='published-pd', storage_size='10Gi')
        c.store[(False, 'storageclasses', 'published-pd')] = {
            'provisioner': 'pd.csi.storage.gke.io', 'allowVolumeExpansion': True}
        assert (await c.resource_info(PROJECT))['min_storage_gi'] == 10
        with pytest.raises(HTTPException) as exc:
            await c.claim('published-' + PROJECT + '-data', '5Gi', runtime=True)
        assert exc.value.status_code == 422
        await c.claim('published-' + PROJECT + '-data', '10Gi', runtime=True)
    asyncio.run(run())


def test_purge_retries_after_pvc_is_still_terminating():
    class DelayedVolumeController(FakeController):
        keep_volume = True

        async def kube(self, method, resource, name='', **kwargs):
            if method == 'DELETE' and resource == 'persistentvolumeclaims' and self.keep_volume:
                return {}
            return await super().kube(method, resource, name, **kwargs)

    async def run():
        c = DelayedVolumeController()
        name = 'published-' + PROJECT
        await c.record(name, {'project_id': PROJECT, 'status': 'stopped'})
        c.store[(True, 'persistentvolumeclaims', name + '-data')] = {'metadata': {'name': name + '-data'}}
        with pytest.raises(HTTPException) as exc:
            await c.purge(PROJECT)
        assert exc.value.status_code == 503
        assert (await c.record(name))['status'] == 'deleting'
        c.keep_volume = False
        assert (await c.purge(PROJECT))['status'] == 'deleted'
        assert await c.record(name) is None
    asyncio.run(run())


def test_push_success_requires_digest_and_timeout_fails():
    async def run():
        c = FakeController()
        await c.submit(BUILD, source()); await c.reconcile()
        c.store[(False, 'jobs', 'build-' + BUILD)]['status'] = {'succeeded': 1}
        async def logs(state): return 'pushing layers\nKOYORINA_IMAGE_DIGEST=' + SHA
        c.build_logs = logs
        await c.reconcile()
        state = await c.record('build-' + BUILD)
        assert state['status'] == 'succeeded' and state['digest'] == SHA
        assert not any(k[1] == 'secrets' and k[2].endswith('-auth') for k in c.store)
        b2 = str(uuid4()); await c.submit(b2, source()); await c.reconcile()
        c.store[(False, 'jobs', 'build-' + b2)]['status'] = {'failed': 1}
        await c.reconcile()
        assert (await c.record('build-' + b2))['status'] == 'failed'
    asyncio.run(run())


def test_publish_verifies_digest_rolls_back_and_retains_data():
    async def run():
        c = FakeController(startup_timeout=30)
        state = await c.submit(BUILD, source())
        state.update(status='succeeded', digest=SHA)
        await c.record('build-' + BUILD, state)
        payload = PublishInput(tenant_id=TENANT, build_id=BUILD,
            image=state['image'].rsplit(':', 1)[0] + '@' + SHA,
            app_origin='https://koyorina.test', forward_secret='f' * 40)
        with pytest.raises(HTTPException):
            await c.publish(PROJECT, payload.model_copy(update={'image': 'evil@' + SHA}))
        result = await c.publish(PROJECT, payload)
        name = 'published-' + PROJECT
        deployment = c.store[(True, 'deployments', name)]
        assert deployment['spec']['replicas'] == 1 and deployment['spec']['strategy']['type'] == 'Recreate'
        assert deployment['spec']['template']['spec']['automountServiceAccountToken'] is False
        deployment['status'] = {'observedGeneration': 1, 'updatedReplicas': 1, 'readyReplicas': 1, 'availableReplicas': 1}
        deployment['metadata']['generation'] = 1
        await c.reconcile()
        assert (await c.record(name))['status'] == 'running'
        await c.publish(PROJECT, payload)
        update = await c.record(name); update['started'] = 0; await c.record(name, update)
        await c.reconcile()
        restored = await c.record(name)
        assert restored['pending']['build_id'] == BUILD
        assert restored['error']
        await c.stop(PROJECT)
        await FakeController(c.store).reconcile()
        assert (True, 'persistentvolumeclaims', name + '-data') in c.store
        assert (True, 'deployments', name) not in c.store
    asyncio.run(run())


def test_stopped_publication_rebinds_builds_without_removing_data():
    async def run():
        c = FakeController()
        move = TenantMoveInput(source_tenant_id=TENANT, target_tenant_id=uuid4(), build_ids=[BUILD])
        build = await c.submit(BUILD, source())
        with pytest.raises(HTTPException):
            await c.move_tenant(PROJECT, move)
        build.update(status='succeeded', digest=SHA)
        await c.record('build-' + BUILD, build)
        image = build['image'].rsplit(':', 1)[0] + '@' + SHA
        payload = PublishInput(tenant_id=TENANT, build_id=BUILD, image=image,
            app_origin='https://koyorina.test', forward_secret='f' * 40)
        await c.publish(PROJECT, payload)
        with pytest.raises(HTTPException):
            await c.move_tenant(PROJECT, move)
        await c.stop(PROJECT)
        claim = (True, 'persistentvolumeclaims', 'published-' + PROJECT + '-data')
        assert claim in c.store
        await c.move_tenant(PROJECT, move)
        await c.move_tenant(PROJECT, move)  # retried requests are safe
        assert (await c.record('build-' + BUILD))['tenant_id'] == str(move.target_tenant_id)
        assert (await c.record('published-' + PROJECT))['tenant_id'] == str(move.target_tenant_id)
        assert claim in c.store
        with pytest.raises(HTTPException):
            await c.publish(PROJECT, payload)
        await c.publish(PROJECT, payload.model_copy(update={'tenant_id': move.target_tenant_id}))
    asyncio.run(run())


def test_controller_rejects_missing_auth():
    with TestClient(create_controller(settings())) as client:
        assert client.get('/healthz').status_code == 200
        assert client.get('/projects/' + PROJECT).status_code == 401


def test_api_grants_environment_catalog_and_cross_tenant(sessions, monkeypatch):
    from backend.config.settings import Settings
    from backend.main import create_app
    import backend.main
    engine = sessions.kw['bind']
    monkeypatch.setattr(backend.main, 'database', lambda _: (engine, sessions))
    cfg = Settings(database_url='postgresql+psycopg://test@localhost/test', app_env='production', app_origin='https://koyorina.test',
        app_session_secret='s' * 40, tenant_secret_key='e' * 40, google_oauth_client_id='test-client')
    app = create_app(cfg)
    with sessions.begin() as db:
        owner = User(email='owner@example.test', role='developer')
        user = User(email='user@example.test', role='user')
        dept = Department(name='sales'); db.add_all([owner, user, dept, Tenant(id=TENANT, name='team')]); db.flush()
        db.add_all([UserTenant(user_id=owner.id, tenant_id=TENANT, role='developer'),
            UserTenant(user_id=user.id, tenant_id=TENANT, role='user')])
        db.add_all([UserTenantRole(user_id=owner.id, tenant_id=TENANT, role='operator'),
                    UserTenantRole(user_id=owner.id, tenant_id=TENANT, role='developer'),
                    UserTenantRole(user_id=user.id, tenant_id=TENANT, role='user')])
        p = Project(id=PROJECT, owner_id=owner.id, tenant_id=TENANT, name='Public', purpose='test',
                    audience='team', fields=[], requirements=[])
        db.add(p); db.flush()
        db.add(AppPublication(project_id=PROJECT, status='running'))
        uid, did, oid = user.id, dept.id, owner.id
    current = {'id': oid}
    def actor(request, db): return db.get(User, current['id'])
    monkeypatch.setattr('backend.api.publication.actor', actor)
    monkeypatch.setattr('backend.api.published_proxy.actor', actor)
    client = TestClient(app, base_url='https://koyorina.test', headers={'Origin': 'https://koyorina.test'})
    base = '/api/projects/' + PROJECT + '/publication'
    saved = client.put(base + '/grants', json={'users': [uid], 'departments': [did]})
    assert saved.status_code == 200, saved.text
    assert client.put(base + '/environment', json={'entries': [{'name': 'MY_KEY', 'value': 'private-value', 'secret': True}]}).status_code == 200
    response = client.get(base + '/environment')
    assert 'private-value' not in response.text
    with sessions() as db:
        assert 'private-value' not in json.dumps(db.get(AppPublication, PROJECT).environment)
    assert client.put(base + '/environment', json={'entries': [{'name': 'DATABASE_URL', 'value': 'evil'}]}).status_code == 422
    assert client.put(base + '/environment', json={'entries': [{'name': 'MY_KEY', 'value': None, 'secret': True}]}).status_code == 200
    current['id'] = uid
    published = client.get('/api/published-apps').json()[0]
    assert published['id'] == PROJECT and published['purpose'] == 'test'
    assert published['tenant_id'] == TENANT and published['tenant_name'] == 'team'
    assert client.put(base + '/grants', json={'users': []}).status_code == 404
    with sessions.begin() as db:
        db.delete(db.get(UserTenant, (uid, TENANT)))
    assert client.get('/api/published-apps').json() == []
    assert client.get('/published-apps/' + PROJECT + '/').status_code == 404


def test_independent_operator_and_user_roles(sessions, monkeypatch):
    from backend.config.settings import Settings
    from backend.main import create_app
    from backend.domain.roles import can_develop_in, can_operate_tenant
    import backend.main
    engine = sessions.kw['bind']
    monkeypatch.setattr(backend.main, 'database', lambda _: (engine, sessions))
    cfg = Settings(database_url='postgresql+psycopg://test@localhost/test', app_env='production',
        app_origin='https://koyorina.test', app_session_secret='s' * 40,
        tenant_secret_key='e' * 40, google_oauth_client_id='test-client')
    app = create_app(cfg)
    with sessions.begin() as db:
        tenant = Tenant(id=TENANT, name='team')
        operator = User(email='operator@example.test', role='member')
        developer = User(email='developer@example.test', role='member')
        visitor = User(email='visitor@example.test', role='member')
        manager = User(email='manager@example.test', role='member')
        db.add_all([tenant, operator, developer, visitor, manager]); db.flush()
        db.add_all([UserTenant(user_id=u.id, tenant_id=TENANT, role=role) for u, role in
            ((operator, 'operator'), (developer, 'developer'), (visitor, 'user'), (manager, 'admin'))])
        db.add_all([UserTenantRole(user_id=u.id, tenant_id=TENANT, role=role) for u, role in
            ((operator, 'operator'), (developer, 'developer'), (visitor, 'user'), (manager, 'admin'))])
        db.add(Project(id=PROJECT, owner_id=developer.id, tenant_id=TENANT, name='Public', purpose='test',
            audience='team', fields=[], requirements=[]))
        published_build = str(uuid4())
        db.add(AppBuild(id=published_build, project_id=PROJECT, tenant_id=TENANT,
            generation_id=str(uuid4()), revision=1, actor_id=developer.id, source_hash='a' * 64,
            registry_kind='private', image=f'registry.test/{PROJECT}:{published_build}', digest=SHA,
            status='succeeded'))
        db.add(AppPublication(project_id=PROJECT, status='running'))
        db.add(PublicationEvent(project_id=PROJECT, build_id=published_build,
            actor_id=developer.id, action='publish'))
        db.flush()
        ids = {'operator': operator.id, 'developer': developer.id,
               'visitor': visitor.id, 'manager': manager.id}
    current = {'id': ids['operator']}
    monkeypatch.setattr('backend.api.publication.actor', lambda request, db: db.get(User, current['id']))
    monkeypatch.setattr('backend.api.published_proxy.actor', lambda request, db: db.get(User, current['id']))
    monkeypatch.setattr('backend.api.masters.actor', lambda request, db: db.get(User, current['id']))
    client = TestClient(app, base_url='https://koyorina.test', headers={'Origin': 'https://koyorina.test'})
    with sessions() as db:
        assert can_operate_tenant(db, db.get(User, ids['operator']), TENANT)
        assert not can_develop_in(db, db.get(User, ids['operator']), TENANT)
    operations = client.get('/api/publication-operations').json()
    assert [row['id'] for row in operations] == [PROJECT]
    assert operations[0]['revision'] == 1
    assert operations[0]['build_created_at']
    assert [row['id'] for row in client.get(f'/api/publication-operations/{PROJECT}/versions').json()] == [published_build]
    assert client.get(f'/api/publication-operations/{PROJECT}/runtime').status_code == 200
    assert client.put(f'/api/publication-operations/{PROJECT}/environment', json={
        'entries': [{'name': 'API_KEY', 'value': 'operator-secret', 'secret': True}]}).status_code == 200
    assert 'operator-secret' not in client.get(
        f'/api/publication-operations/{PROJECT}/environment').text
    assert client.put(f'/api/publication-operations/{PROJECT}/grants',
        json={'users': [ids['visitor']], 'departments': []}).status_code == 200
    assert client.get('/api/published-apps').json() == []  # operator is not a user
    current['id'] = ids['developer']
    assert client.get('/api/publication-operations').json() == []
    assert client.get(f'/api/publication-operations/{PROJECT}/runtime').status_code == 404
    assert client.get(f'/api/publication-operations/{PROJECT}/environment').status_code == 404
    assert client.put(f'/api/projects/{PROJECT}/publication/environment', json={
        'entries': [{'name': 'API_KEY', 'value': 'developer-secret', 'secret': True}]}).status_code == 404
    assert client.put(f'/api/publication-operations/{PROJECT}/grants',
        json={'users': [], 'departments': []}).status_code == 404
    current['id'] = ids['visitor']
    assert [row['id'] for row in client.get('/api/published-apps').json()] == [PROJECT]
    current['id'] = ids['manager']
    assert client.get('/api/publication-operations').json() == []
    assert client.get(f'/api/tenants/{TENANT}/members').status_code == 200
    changed = client.put(f'/api/tenants/{TENANT}/members/{ids["visitor"]}/roles',
        json={'roles': ['operator']})
    assert changed.status_code == 200, changed.text
    current['id'] = ids['visitor']
    assert client.get('/api/published-apps').json() == []
    assert [row['id'] for row in client.get('/api/publication-operations').json()] == [PROJECT]


def test_operator_starts_only_released_version_with_saved_environment(sessions, monkeypatch):
    from backend.config.settings import Settings
    from backend.main import create_app
    import backend.main
    engine = sessions.kw['bind']
    monkeypatch.setattr(backend.main, 'database', lambda _: (engine, sessions))
    cfg = Settings(database_url='postgresql+psycopg://test@localhost/test', app_env='production',
        app_origin='https://koyorina.test', app_session_secret='s' * 40,
        tenant_secret_key='e' * 40, google_oauth_client_id='test-client')
    cfg = cfg.model_copy(update={'publication_enabled': True})
    app = create_app(cfg)
    build_id, unregistered_build_id = str(uuid4()), str(uuid4())
    with sessions.begin() as db:
        operator = User(email='operator@example.test', role='member')
        developer = User(email='developer@example.test', role='member')
        db.add_all([Tenant(id=TENANT, name='team'), operator, developer]); db.flush()
        for user, role in ((operator, 'operator'), (developer, 'developer')):
            db.add(UserTenant(user_id=user.id, tenant_id=TENANT, role=role))
            db.add(UserTenantRole(user_id=user.id, tenant_id=TENANT, role=role))
        db.add(Project(id=PROJECT, owner_id=developer.id, tenant_id=TENANT, name='Public',
            purpose='test', audience='team', fields=[], requirements=[]))
        db.add(AppBuild(id=build_id, project_id=PROJECT, tenant_id=TENANT,
            generation_id=str(uuid4()), revision=1, actor_id=developer.id, source_hash='a' * 64,
            registry_kind='private', image=f'registry.test/{PROJECT}:{build_id}', digest=SHA,
            status='succeeded'))
        db.add(AppBuild(id=unregistered_build_id, project_id=PROJECT, tenant_id=TENANT,
            generation_id=str(uuid4()), revision=2, actor_id=developer.id, source_hash='b' * 64,
            registry_kind='private', image=f'registry.test/{PROJECT}:{unregistered_build_id}', digest=SHA,
            status='succeeded'))
        ids = {'operator': operator.id, 'developer': developer.id}
    current = {'id': ids['operator']}
    monkeypatch.setattr('backend.api.publication.actor', lambda request, db: db.get(User, current['id']))
    captured = []
    async def controller(settings, method, path, payload=None):
        if method == 'GET' and path.endswith('/resources'):
            return {'defaults': {'cpu_request_m': 100, 'cpu_limit_m': 1000,
                'memory_request_mi': 256, 'memory_limit_mi': 1024, 'storage_gi': 10},
                'pvc_size': None, 'expandable': None, 'min_storage_gi': 10}
        if method == 'POST':
            captured.append(payload)
        return {'status': 'starting'} if method == 'POST' else None
    monkeypatch.setattr('backend.api.publication.call', controller)
    client = TestClient(app, base_url='https://koyorina.test', headers={'Origin': 'https://koyorina.test'})
    listed = client.get('/api/publication-operations').json()
    assert listed == []
    assert client.post(f'/api/publication-operations/{PROJECT}/start', json={}).status_code == 409
    assert client.post(f'/api/projects/{PROJECT}/publication/release',
        json={'build_id': build_id}).status_code == 404  # 運用者は開発者の公開登録を代行しない
    assert client.get(f'/api/publication-operations/{PROJECT}/versions').json() == []
    current['id'] = ids['developer']
    assert client.post(f'/api/projects/{PROJECT}/publication', json={'build_id': build_id}).status_code == 404
    released = client.post(f'/api/projects/{PROJECT}/publication/release', json={'build_id': build_id})
    assert released.status_code == 202, released.text
    assert captured == []  # 登録時は実行基盤を起動しない
    current['id'] = ids['operator']
    listed = client.get('/api/publication-operations').json()
    assert [(row['id'], row['status']) for row in listed] == [(PROJECT, 'stopped')]
    assert listed[0]['startable'] and listed[0]['candidate_build_id'] == build_id
    assert [row['id'] for row in client.get(f'/api/publication-operations/{PROJECT}/versions').json()] == [build_id]
    resources_path = f'/api/publication-operations/{PROJECT}/resources'
    assert client.get(resources_path).json()['values']['storage_gi'] == 10
    configured = {'cpu_request_m': 250, 'cpu_limit_m': 750,
        'memory_request_mi': 512, 'memory_limit_mi': 1536, 'storage_gi': 15}
    assert client.put(resources_path, json={**configured, 'storage_gi': 5}).status_code == 409
    assert client.put(resources_path, json={**configured, 'cpu_request_m': 225}).status_code == 422
    assert client.put(resources_path, json={**configured, 'memory_request_mi': 384}).status_code == 422
    assert client.put(resources_path, json={**configured, 'memory_limit_mi': 1408}).status_code == 422
    assert client.put(resources_path, json={**configured, 'storage_gi': 12}).status_code == 422
    assert client.put(resources_path, json=configured).status_code == 200
    assert client.get(resources_path).json()['values'] == configured
    assert client.post(f'/api/projects/{PROJECT}/publication',
        json={'build_id': unregistered_build_id}).status_code == 409
    environment_path = f'/api/publication-operations/{PROJECT}/environment'
    assert client.put(environment_path, json={'entries': [
        {'name': 'API_KEY', 'value': 'operator-secret', 'secret': True}]}).status_code == 200
    assert client.put(environment_path, json={'entries': [
        {'name': 'API_KEY', 'value': None, 'secret': True}]}).status_code == 200
    assert client.post(f'/api/publication-operations/{PROJECT}/start', json={}).status_code == 202
    assert captured[-1]['environment'] == {'API_KEY': 'operator-secret'}
    assert captured[-1]['resources'] == configured


def test_proxy_replaces_identity_headers_and_scopes_cookies(sessions, monkeypatch):
    from backend.api import published_proxy
    from backend.domain.preview import cookie_prefix
    from fastapi import FastAPI
    from starlette.middleware.sessions import SessionMiddleware
    from uuid import UUID
    import httpx
    with sessions.begin() as db:
        u = User(email='user@example.test', role='user'); db.add(u)
        db.add(Tenant(id=TENANT, name='team')); db.flush()
        uid = u.id
        p = Project(id=PROJECT, owner_id=uid, tenant_id=TENANT, name='App', purpose='test', audience='team', fields=[])
        db.add(p); db.flush()
        db.add_all([UserTenant(user_id=uid, tenant_id=TENANT), AppPublication(project_id=PROJECT, status='running'),
            PublicationGrant(project_id=PROJECT, kind='user', subject_id=uid)])
    app = FastAPI(); app.include_router(published_proxy.router)
    app.add_middleware(SessionMiddleware, secret_key='x' * 40)
    app.state.sessions = sessions; app.state.session_cookie = 'session'
    app.state.settings = SimpleNamespace(app_name='koyorina', app_session_secret='s' * 40)
    monkeypatch.setattr(published_proxy, 'actor', lambda request, db: db.get(User, uid))
    async def call(*args): return {'status': 'running'}
    monkeypatch.setattr(published_proxy, 'call', call)
    requests = []
    class Upstream:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def request(self, method, url, **kwargs):
            requests.append((url, kwargs))
            return httpx.Response(200, content=b'ok', headers={'Set-Cookie': 'sid=value; Path=/; HttpOnly',
                'Location': '/published-apps/' + PROJECT + '/target'})
    monkeypatch.setattr(published_proxy, 'httpx', SimpleNamespace(AsyncClient=Upstream, HTTPError=httpx.HTTPError))
    client = TestClient(app)
    result = client.get('/published-apps/' + PROJECT + '/', headers={
        'X-Forge-Auth': 'fake', 'X-Forge-User-Email': 'attacker@example.test',
        'Cookie': 'session=private; ' + cookie_prefix(UUID(PROJECT)) + 'preview=private'}, follow_redirects=False)
    assert result.status_code == 200
    headers = requests[0][1]['headers']
    assert headers['X-Forge-Auth'] == published_secret(PROJECT, 's' * 40)
    assert headers['X-Forge-User-Email'] == 'user@example.test'
    assert 'Cookie' not in headers
    assert 'Path=/published-apps/' + PROJECT + '/' in result.headers['set-cookie']
    assert result.headers['location'] == '/published-apps/' + PROJECT + '/target'


def test_both_registry_modes_use_private_or_short_lived_credentials(monkeypatch):
    async def run():
        private = FakeController()
        assert await private.credentials('auth') == 'push'
        artifact = FakeController()
        artifact.settings = settings().model_copy(update={'registry_kind': 'artifact',
            'registry_host': 'asia-northeast1-docker.pkg.dev/project-test/koyorina-apps'})
        import google.auth
        class Credentials:
            token = 'temporary-token'
            def refresh(self, request): pass
        monkeypatch.setattr(google.auth, 'default', lambda **kwargs: (Credentials(), 'project-test'))
        assert await artifact.credentials('auth') == 'auth'
        secret = artifact.store[(False, 'secrets', 'auth')]
        config = json.loads(base64.b64decode(secret['data']['.dockerconfigjson']))
        auth = config['auths']['asia-northeast1-docker.pkg.dev']['auth']
        assert base64.b64decode(auth).decode() == 'oauth2accesstoken:temporary-token'
    asyncio.run(run())


def test_incomplete_snapshot_is_never_built():
    async def run():
        c = FakeController()
        state = await c.submit(BUILD, source())
        state.update(snapshot_ready=False, created=0)
        await c.record('build-' + BUILD, state)
        await c.reconcile()
        assert (await c.record('build-' + BUILD))['status'] == 'failed'
        assert not any(k[1] == 'jobs' for k in c.store)
    asyncio.run(run())


def test_migration_adds_tables_without_touching_existing_records(sessions):
    import importlib.util
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import inspect
    spec = importlib.util.spec_from_file_location('publication_migration',
        'backend/migrations/versions/0002_publication.py')
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    engine = sessions.kw['bind']
    with engine.begin() as conn:
        for model in (PublicationGrant, AppPublication, AppBuild):
            model.__table__.drop(conn)
        from backend.core.db import PublicationEvent
        # Event has no FK to builds, so it may be dropped independently.
        PublicationEvent.__table__.drop(conn)
        module.op = Operations(MigrationContext.configure(conn))
        module.upgrade()
        assert {'app_builds', 'app_publications', 'publication_grants', 'publication_events'}.issubset(inspect(conn).get_table_names())
        module.downgrade()
        assert 'projects' in inspect(conn).get_table_names()


def test_api_build_push_does_not_publish_and_manual_release_is_separate(sessions, monkeypatch):
    from backend.config.settings import Settings
    from backend.main import create_app
    import backend.main
    from backend.core.db import Audit
    engine = sessions.kw['bind']
    monkeypatch.setattr(backend.main, 'database', lambda _: (engine, sessions))
    cfg = Settings(database_url='postgresql+psycopg://test@localhost/test', app_env='production',
        app_origin='https://koyorina.test', app_session_secret='s' * 40, google_oauth_client_id='test')
    cfg = cfg.model_copy(update={'publication_enabled': True, 'local_codex_enabled': True})
    app = create_app(cfg)
    with sessions.begin() as db:
        owner = User(email='owner@example.test', role='developer'); db.add(owner)
        db.add(Tenant(id=TENANT, name='team')); db.flush()
        db.add(UserTenant(user_id=owner.id, tenant_id=TENANT, role='developer'))
        db.add_all([UserTenantRole(user_id=owner.id, tenant_id=TENANT, role='developer'),
                    UserTenantRole(user_id=owner.id, tenant_id=TENANT, role='operator')])
        db.add(Project(id=PROJECT, owner_id=owner.id, tenant_id=TENANT, name='App', purpose='test', audience='team', fields=[]))
        db.flush()
        job = GenerationJob(project_id=PROJECT, owner_id=owner.id, revision=1, specification={},
            source_type='local_codex', artifact=copy.deepcopy(BUNDLE), status='generated')
        db.add(job); db.flush()
        uid, jid = owner.id, job.id
    monkeypatch.setattr('backend.api.publication.actor', lambda request, db: db.get(User, uid))
    states = {}
    captured = []
    async def call(settings, method, path, payload=None):
        if method == 'POST' and path.startswith('/builds/'):
            captured.append(copy.deepcopy(payload)); states[path] = {'status': 'queued'}
            return states[path]
        if method == 'POST' and path.startswith('/projects/'):
            captured.append(copy.deepcopy(payload)); states[path] = {'status': 'starting', 'build_id': None}
            return states[path]
        if method == 'DELETE':
            if path.endswith('/data'):
                states.pop('/projects/' + PROJECT, None)
                return {'status': 'deleted'}
            states[path] = {'status': 'stopped'}; return states[path]
        return states.get(path)
    monkeypatch.setattr('backend.api.publication.call', call)
    client = TestClient(app, base_url='https://koyorina.test', headers={'Origin': 'https://koyorina.test'})
    base = '/api/projects/' + PROJECT
    response = client.post(base + '/builds', json={'generation_id': jid})
    assert response.status_code == 202, response.text
    bid = response.json()['id']
    assert client.post(base + '/builds', json={'generation_id': jid}).status_code == 409
    original_source = captured[0]['source']
    assert captured[0]['revision'] == 1
    with sessions.begin() as db:
        db.get(GenerationJob, jid).artifact = {'files': []}
    assert captured[0]['source'] == original_source
    assert client.post(base + '/publication', json={'build_id': bid}).status_code == 409
    assert client.post(base + '/publication/release', json={'build_id': bid}).status_code == 409
    states['/builds/' + bid] = {'status': 'succeeded', 'digest': SHA}
    status = client.get(base + '/publication').json()
    assert status['status'] == 'stopped'
    assert status['builds'][0]['status'] == 'succeeded'
    with sessions() as db:
        assert db.get(AppPublication, PROJECT) is None
    assert client.get('/api/publication-operations').json() == []
    assert client.post(base + '/publication/release', json={'build_id': bid}).status_code == 202
    assert len(captured) == 1  # 登録後もビルド要求以外は送らない
    states['/projects/' + PROJECT] = {'status': 'stopped', 'build_id': str(uuid4())}
    assert client.get(base + '/publication').json()['build_id'] == bid
    assert client.get('/api/publication-operations').json()[0]['startable']
    assert client.post(f'/api/publication-operations/{PROJECT}/start', json={}).status_code == 202
    assert captured[-1]['image'].endswith('@' + SHA)
    assert captured[-1]['environment'] == {}
    assert 'DATABASE_URL' not in captured[-1]
    states['/projects/' + PROJECT] = {'status': 'running', 'build_id': bid}
    assert client.get(base + '/publication').json()['status'] == 'running'
    async def rejecting_call(settings, method, path, payload=None):
        if method == 'POST' and path.startswith('/projects/'):
            raise HTTPException(422, 'Registryのノード同期基盤が未配備です。Ansibleを再適用してください。')
        return await call(settings, method, path, payload)
    monkeypatch.setattr('backend.api.publication.call', rejecting_call)
    rejected = client.post(base + '/publication', json={'build_id': bid})
    assert rejected.status_code == 422
    assert 'ノード同期基盤' in rejected.json()['error']
    with sessions() as db:
        assert db.get(AppPublication, PROJECT).status == 'running'
    monkeypatch.setattr('backend.api.publication.call', call)
    # PostgreSQLのvarchar(300)制限をSQLiteでも検証する。
    from sqlalchemy import event
    def enforce_error_limit(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith('UPDATE app_publications'):
            assert all(not isinstance(value, str) or len(value) <= 300 for value in parameters)
    event.listen(engine, 'before_cursor_execute', enforce_error_limit)
    long_error = 'ImagePullBackOff: ' + 'image-path/' * 80 + 'http: server gave HTTP response to HTTPS client'
    states['/projects/' + PROJECT] = {'status': 'failed', 'build_id': bid, 'error': long_error}
    response = client.get(base + '/publication')
    assert response.status_code == 200, response.text
    assert response.json()['status'] == 'failed'
    assert len(response.json()['error']) == 300
    assert response.json()['error'].startswith('ImagePullBackOff:')
    assert response.json()['error'].endswith('http: server gave HTTP response to HTTPS client')
    event.remove(engine, 'before_cursor_execute', enforce_error_limit)
    assert client.delete(base + '/publication', headers={'Content-Type': 'application/json'}).status_code == 200
    with sessions() as db:
        assert db.get(AppPublication, PROJECT).status == 'stopped'
        assert db.get(AppBuild, bid).digest == SHA
        actions = set(db.scalars(select(Audit.action)))
        assert {'build.requested', 'build.succeeded', 'publication.released',
                'publication.start_requested', 'publication.running', 'publication.stopped'} <= actions
    restarted = client.post(f'/api/publication-operations/{PROJECT}/start', json={})
    assert restarted.status_code == 202, restarted.text
    with sessions() as db:
        assert 'publication.start_requested' in set(db.scalars(select(Audit.action)))
    with sessions.begin() as db:
        db.get(AppPublication, PROJECT).environment = [{'name': 'API_KEY', 'secret': True, 'value': 'hidden'}]
        db.add(PublicationGrant(project_id=PROJECT, kind='user', subject_id=uid))
    assert client.delete(f'/api/publication-operations/{PROJECT}',
        headers={'Content-Type': 'application/json'}).status_code == 409
    states['/projects/' + PROJECT] = {'status': 'stopped', 'build_id': bid}
    deleted = client.delete(f'/api/publication-operations/{PROJECT}',
        headers={'Content-Type': 'application/json'})
    assert deleted.status_code == 200, deleted.text
    with sessions() as db:
        assert db.get(AppPublication, PROJECT) is None
        assert db.get(AppBuild, bid).digest == SHA
        assert db.scalars(select(PublicationGrant).where(PublicationGrant.project_id == PROJECT)).all() == []
        assert 'publication.deleted' in set(db.scalars(select(Audit.action)))
        assert 'delete' in set(db.scalars(select(PublicationEvent.action).where(PublicationEvent.project_id == PROJECT)))
    assert client.get('/api/publication-operations').json() == []
    assert client.post(base + '/publication/release', json={'build_id': bid}).status_code == 202


@pytest.mark.parametrize('http', [True, False])
def test_build_registry_transport_applies_to_push_and_cache(http):
    async def run():
        c = FakeController(registry_http=http)
        await c.submit(BUILD, source())
        await c.reconcile()
        pod = c.store[(False, 'jobs', 'build-' + BUILD)]['spec']['template']['spec']
        builder = pod['containers'][0]
        env = {v['name']: v['value'] for v in builder['env']}
        assert env['REGISTRY_HTTP'] == str(http).lower()
        assert '--config /tmp/buildkitd.toml' in env['BUILDKITD_FLAGS']
        script = builder['command'][2]
        assert 'http = %s' in script
        assert 'mode=max,registry.insecure="$REGISTRY_HTTP"' in script
        assert 'push=true,registry.insecure=$REGISTRY_HTTP' in script
        assert not any(v['name'] == 'registry-ca' for v in pod['volumes'])
    asyncio.run(run())


def test_artifact_registry_rejects_http():
    with pytest.raises(ValueError, match='HTTP is only supported'):
        settings(registry_kind='artifact', registry_http=True)


def artifact_selection(**overrides):
    from backend.domain.system_registry import RegistrySelection
    return RegistrySelection(kind='artifact', host='asia-northeast1-docker.pkg.dev/my-project/koyorina-apps',
        wif_project_number='123456789012', wif_pool_id='koyorina', wif_provider_id='k3s-publication',
        writer_service_account='builder@my-project.iam.gserviceaccount.com',
        reader_service_account='reader@my-project.iam.gserviceaccount.com', **overrides)


def test_registry_selection_accepts_private_destination():
    from backend.domain.system_registry import RegistrySelection
    selection = RegistrySelection(kind='private', host='registry.test:5000', username='builder', scanning_enabled=True,
                                  writer_service_account='ignored')
    assert selection.host == 'registry.test:5000' and not selection.scanning_enabled
    assert selection.writer_service_account == ''


def test_wif_rejects_same_writer_reader_and_wrong_host():
    from backend.domain.system_registry import RegistrySelection
    values = artifact_selection().model_dump()
    for changes in ({'reader_service_account': values['writer_service_account']}, {'host': 'https://evil.test'},
                    {'wif_project_number': 'invalid'}, {'writer_service_account': 'service-account-key.json'}):
        with pytest.raises(ValueError):
            RegistrySelection.model_validate({**values, **changes})


def test_wif_service_accounts_are_optional_per_subject():
    from backend.domain.system_registry import RegistrySelection
    values = artifact_selection().model_dump()
    direct = RegistrySelection.model_validate({**values,
        'writer_service_account': '', 'reader_service_account': ''})
    assert 'service_account_impersonation_url' not in direct.credential_config()
    assert 'service_account_impersonation_url' not in direct.credential_config(True)
    mixed = RegistrySelection.model_validate({**values, 'reader_service_account': ''})
    assert 'service_account_impersonation_url' in mixed.credential_config()
    assert 'service_account_impersonation_url' not in mixed.credential_config(True)
    assert RegistrySelection.model_validate({**values, 'writer_service_account': ''}).reader_service_account


def test_wif_audience_and_impersonation_are_fixed_google_endpoints():
    registry = artifact_selection()
    assert registry.audience().startswith('https://iam.googleapis.com/projects/123456789012/')
    assert registry.credential_config()['token_url'] == 'https://sts.googleapis.com/v1/token'
    assert '/reader@my-project.iam.gserviceaccount.com:' in registry.credential_config(True)['service_account_impersonation_url']


def test_artifact_build_snapshot_and_pull_refresh_survive_switch_and_restart():
    async def run():
        c = FakeController(registry_http=True)
        registry = artifact_selection()
        async def token(selection, reader=False):
            assert selection.host == registry.host
            return 'reader-token' if reader else 'writer-token'
        c.access_token = token
        state = await c.submit(BUILD, source().model_copy(update={'registry': registry}))
        assert state['image'].startswith(registry.host + '/')
        await c.reconcile()
        pod = c.store[(False, 'jobs', 'build-' + BUILD)]['spec']['template']['spec']
        env = {e['name']: e['value'] for e in pod['containers'][0]['env']}
        assert env['REGISTRY_HTTP'] == 'false' and env['CACHE_IMAGE'].startswith(registry.host)
        state.update(status='succeeded', digest=SHA)
        await c.record('build-' + BUILD, state)
        payload = PublishInput(tenant_id=TENANT, build_id=BUILD,
            image=state['image'].rsplit(':', 1)[0] + '@' + SHA, app_origin='https://koyorina.test', forward_secret='f' * 40)
        await c.publish(PROJECT, payload)
        published = await c.record('published-' + PROJECT)
        pull_name = published['pending']['manifest']['spec']['template']['spec']['imagePullSecrets'][0]['name']
        secret = c.store[(True, 'secrets', pull_name)]
        docker = json.loads(base64.b64decode(secret['data']['.dockerconfigjson']))
        assert base64.b64decode(next(iter(docker['auths'].values()))['auth']).decode() == 'oauth2accesstoken:reader-token'
        await c.cleanup_build(state)
        assert (False, 'secrets', 'build-' + BUILD + '-auth') not in c.store
        assert (True, 'secrets', pull_name) in c.store
        restarted = FakeController(c.store)
        restarted.access_token = token
        await restarted.reconcile()
        assert pull_name in restarted.pull_refreshed
        other = str(uuid4())
        private = await restarted.submit(other, source())
        assert private['image'].startswith('registry.example.test:30500/')
        assert (await restarted.record('build-' + BUILD))['registry']['host'] == registry.host
    asyncio.run(run())


def test_registry_settings_admin_only_and_persisted(sessions, monkeypatch):
    from backend.config.settings import Settings
    from backend.main import create_app
    import backend.main
    from backend.core.db import SystemSetting
    engine = sessions.kw['bind']
    monkeypatch.setattr(backend.main, 'database', lambda _: (engine, sessions))
    cfg = Settings(database_url='postgresql+psycopg://test@localhost/test', app_env='local',
                   tenant_secret_key='k' * 40, app_session_secret='s' * 40, app_origin='http://localhost:8080')
    app = create_app(cfg)
    with sessions.begin() as db:
        admin = User(email='admin@test.example', role='admin')
        user = User(email='user@test.example', role='developer')
        db.add_all([admin, user]); db.flush(); admin_id, user_id = admin.id, user.id
    import backend.api.system_settings as system_api
    with TestClient(app, base_url='http://localhost:8080', headers={'Origin': 'http://localhost:8080'}) as client:
        def as_user(request, db):
            return db.get(User, user_id)
        monkeypatch.setattr(system_api, 'actor', as_user)
        assert client.get('/api/system/app-registry').status_code == 403
        assert client.put('/api/system/app-registry', json=artifact_selection().model_dump()).status_code == 403
        monkeypatch.setattr(system_api, 'actor', lambda request, db: db.get(User, admin_id))
        assert client.put('/api/system/app-registry', json=artifact_selection().model_dump()).status_code == 200
        assert client.get('/api/system/app-registry').json()['selection']['kind'] == 'artifact'
        direct = {**artifact_selection().model_dump(),
                  'writer_service_account': '', 'reader_service_account': ''}
        assert client.put('/api/system/app-registry', json=direct).status_code == 200
        visible = client.get('/api/system/app-registry').json()['selection']
        assert visible['writer_service_account'] == visible['reader_service_account'] == ''
        custom = {'kind': 'private', 'host': 'registry.test:5000', 'username': 'builder', 'password': 'private-password', 'http': True}
        saved = client.put('/api/system/app-registry', json=custom)
        assert saved.status_code == 200
        assert 'private-password' not in saved.text
        result = client.get('/api/system/app-registry')
        assert result.json()['selection']['password_configured']
        assert 'private-password' not in result.text
        with sessions() as db:
            assert 'private-password' not in json.dumps(db.get(SystemSetting, 'app-registry').value)
        custom['password'] = ''
        assert client.put('/api/system/app-registry', json=custom).status_code == 200
        custom['host'] = 'other.test:5000'
        assert client.put('/api/system/app-registry', json=custom).status_code == 422
        assert client.put('/api/system/app-registry', json={'kind': 'private'}).status_code == 200
        with sessions() as db:
            assert db.get(SystemSetting, 'app-registry').value['kind'] == 'private'
            assert db.scalar(select(Audit).where(Audit.action == 'system.registry_updated'))


def test_artifact_timeout_is_shorter_than_token_lifetime_on_private_infrastructure():
    async def run():
        c = FakeController(build_timeout=7200)
        async def token(registry, reader=False):
            return 'token'
        c.access_token = token
        await c.submit(BUILD, source().model_copy(update={'registry': artifact_selection()}))
        await c.reconcile()
        assert c.store[(False, 'jobs', 'build-' + BUILD)]['spec']['activeDeadlineSeconds'] == 3000
        assert (await c.record('build-' + BUILD))['build_timeout'] == 3000
    asyncio.run(run())


def test_token_request_uses_separate_fixed_kubernetes_identities(monkeypatch):
    from backend.core import k8s_token
    from types import SimpleNamespace
    calls = []
    async def run():
        c = FakeController()
        async def kube(method, resource, name='', **kwargs):
            calls.append((method, resource, name, kwargs['body']['spec']))
            return {'status': {'token': 'signed-oidc-token'}}
        c.kube = kube
        def credentials(config, supplier):
            assert supplier.get_subject_token(None, None) == 'signed-oidc-token'
            return SimpleNamespace(refresh=lambda _: None, token='access-token')
        monkeypatch.setattr(k8s_token, 'credentials', credentials)
        assert await c.access_token(artifact_selection()) == 'access-token'
        assert await c.access_token(artifact_selection(), reader=True) == 'access-token'
    asyncio.run(run())
    assert [c[2] for c in calls] == ['publication-controller/token', 'publication-puller/token']
    assert all(c[3]['audiences'] == [artifact_selection().audience()] for c in calls)
    assert all(c[3]['expirationSeconds'] == 3600 for c in calls)


def test_connection_check_rejects_writer_without_upload_permission(monkeypatch):
    import httpx
    class Client:
        def __init__(self, **kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def get(self, url, **kwargs):
            return httpx.Response(200, json={'format': 'DOCKER'})
        async def post(self, url, **kwargs):
            assert url.endswith(':testIamPermissions')
            return httpx.Response(200, json={'permissions': ['artifactregistry.repositories.downloadArtifacts']})
    async def run():
        c = FakeController()
        async def token(registry, reader=False):
            return 'token'
        c.access_token = token
        monkeypatch.setattr('backend.worker.publication_controller.httpx.AsyncClient', Client)
        result = await c.registry_test(artifact_selection())
        assert not result['ok'] and '権限' in result['message']
    asyncio.run(run())


def test_private_registry_password_encrypted_retained_and_hidden():
    from backend.domain import system_registry as registry
    key = 'k' * 40
    payload = registry.RegistrySelection(kind='private', host='registry.test:5000', username='builder', password=' secret ', http=True)
    stored = registry.apply(None, payload, key)
    assert ' secret ' not in json.dumps(stored)
    assert registry.visible(stored)['password'] == ''
    assert registry.visible(stored)['password_configured']
    assert registry.selection(stored, key).password == ' secret '
    payload.password = ''
    assert registry.apply(stored, payload, key) == stored
    payload.host = 'other.test:5000'
    with pytest.raises(ValueError):
        registry.apply(stored, payload, key)


def test_private_registry_snapshot_only_references_kubernetes_secret():
    from backend.domain.system_registry import RegistrySelection
    async def check():
        controller = FakeController()
        registry = RegistrySelection(kind='private', host='registry.test:5000', username='builder', password='password-value', http=True)
        state = await controller.submit(BUILD, source().model_copy(update={'registry': registry}))
        assert 'password-value' not in json.dumps(state)
        assert state['image'].startswith('registry.test:5000/')
        await controller.start_build(state)
        pod = controller.store[(False, 'jobs', 'build-' + BUILD)]['spec']['template']['spec']
        env = {entry['name']: entry['value'] for entry in pod['containers'][0]['env']}
        assert env['REGISTRY_HTTP'] == 'true' and env['CACHE_IMAGE'].startswith('registry.test:5000/')
        name = await controller.credentials('unused', state['registry'])
        pull = await controller.ensure_pull(state['registry'])
        assert pull == name
        config = controller.store[(True, 'secrets', pull)]['data']['.dockerconfigjson']
        assert base64.b64decode(json.loads(base64.b64decode(config))['auths']['registry.test:5000']['auth']).decode() == 'builder:password-value'
    asyncio.run(check())


def test_registry_api_missing_reports_controller_version_mismatch(monkeypatch):
    import httpx
    from backend.core import publication_client
    original = httpx.AsyncClient
    transport = httpx.MockTransport(lambda request: httpx.Response(404, json={'detail': 'Not Found'}))
    monkeypatch.setattr(publication_client.httpx, 'AsyncClient',
                        lambda **kwargs: original(transport=transport, **kwargs))
    settings = SimpleNamespace(publication_enabled=True, publication_controller_url='http://controller.test',
                               publication_controller_token=SimpleNamespace(get_secret_value=lambda: 'token'))
    async def check():
        with pytest.raises(HTTPException) as error:
            await publication_client.call(settings, 'POST', '/registry/test', {})
        assert error.value.status_code == 503
        assert '同じ版' in error.value.detail
        assert await publication_client.call(settings, 'GET', '/builds/missing') is None
    asyncio.run(check())


def test_startup_failure_keeps_pull_reason_after_deployment_cleanup():
    async def run():
        c = FakeController(startup_timeout=30)
        build = await c.submit(BUILD, source())
        build.update(status='succeeded', digest=SHA)
        await c.record('build-' + BUILD, build)
        await c.publish(PROJECT, PublishInput(tenant_id=TENANT, build_id=BUILD,
            image=build['image'].rsplit(':', 1)[0] + '@' + SHA,
            app_origin='https://koyorina.test', forward_secret='f' * 40))
        name = 'published-' + PROJECT
        c.store[(True, 'pods', 'failed-pod')] = {'metadata': {'name': 'failed-pod',
            'labels': {'koyorina-published': name}}, 'status': {'containerStatuses': [
            {'state': {'waiting': {'reason': 'ImagePullBackOff',
             'message': 'HTTP response to HTTPS client password=hidden'}}}]}}
        state = await c.record(name)
        state['started'] = 0
        await c.tick_publication(name, state)
        failed = await c.record(name)
        assert failed['status'] == 'failed'
        assert 'ImagePullBackOff' in failed['error']
        assert 'HTTP response to HTTPS client' in failed['error']
        assert 'hidden' not in failed['error']
        assert (True, 'deployments', name) not in c.store
        assert (True, 'persistentvolumeclaims', name + '-data') in c.store
    asyncio.run(run())


@pytest.mark.parametrize('value', [None, '', '短い理由', 'a' * 300, 'a' * 301, '失敗' * 1000])
def test_stored_error_respects_database_limit(value):
    from backend.api.publication import stored_error
    saved = stored_error(value)
    assert saved is None if value is None else len(saved) <= 300
    if value is not None and len(value) <= 300:
        assert saved == value


def test_stored_error_masks_secrets_before_shortening():
    from backend.api.publication import stored_error
    saved = stored_error('ImagePullBackOff: token=private ' + 'x' * 400 + ' password=hidden')
    assert 'private' not in saved and 'hidden' not in saved
    assert saved.endswith('password=***')


def test_build_dockerfile_is_pinned_and_survives_build_cleanup(tmp_path):
    import shutil
    async def check():
        for file in ('Dockerfile.generated', 'front.py', 'entrypoint.sh', 'install.py'):
            shutil.copy(Path('setup/publication') / file, tmp_path / file)
        c = FakeController()
        c.settings = c.settings.model_copy(update={'assets': tmp_path})
        original = (tmp_path / 'Dockerfile.generated').read_text()
        assert 'ARG SECURITY_UPDATE_ID' in original
        assert 'apt-get upgrade -y' in original
        assert 'libsqlite3-0' in original
        build = await c.submit(BUILD, source())
        (tmp_path / 'Dockerfile.generated').write_text('# New platform version\n' + original)
        await c.start_build(build)
        platform = c.store[(False, 'configmaps', 'build-' + BUILD + '-platform')]
        assert platform['data']['Dockerfile'] == original
        await c.cleanup_build(build)
        assert await c.build_dockerfile(build) == {'dockerfile': original, 'recorded': True}
        assert (await c.build_dockerfile())['dockerfile'].startswith('# New platform version')
        legacy = {'id': str(uuid4())}
        assert await c.build_dockerfile(legacy) == {'dockerfile': '', 'recorded': False}
    asyncio.run(check())


def test_dockerfile_endpoints_enforce_project_and_build_access(sessions, monkeypatch):
    from backend.config.settings import Settings
    from backend.main import create_app
    import backend.main
    engine = sessions.kw['bind']
    monkeypatch.setattr(backend.main, 'database', lambda _: (engine, sessions))
    config = Settings(database_url='postgresql+psycopg://test@localhost/test', app_env='production',
        app_origin='https://koyorina.test', app_session_secret='s' * 40, google_oauth_client_id='test')
    app = create_app(config.model_copy(update={'publication_enabled': True}))
    with sessions.begin() as db:
        owner = User(email='recipe-owner@test.example', role='developer')
        outsider = User(email='recipe-outsider@test.example', role='developer')
        db.add_all([owner, outsider]); db.add(Tenant(id=TENANT, name='team')); db.flush()
        db.add(UserTenant(user_id=owner.id, tenant_id=TENANT, role='developer'))
        db.add(Project(id=PROJECT, owner_id=owner.id, tenant_id=TENANT, name='App', purpose='test', audience='team', fields=[])); db.flush()
        db.add(AppBuild(id=BUILD, project_id=PROJECT, tenant_id=TENANT, generation_id=str(uuid4()),
            revision=1, actor_id=owner.id, source_hash='a' * 64, registry_kind='private', image='registry.test/app:tag'))
        uid, other = owner.id, outsider.id
    acting = [uid]
    monkeypatch.setattr('backend.api.publication.actor', lambda request, db: db.get(User, acting[0]))
    calls = []
    async def call(settings, method, path, payload=None):
        calls.append(path)
        return {'dockerfile': 'FROM python:3.14-slim\n', 'recorded': path.startswith('/builds/')}
    monkeypatch.setattr('backend.api.publication.call', call)
    client = TestClient(app, base_url='https://koyorina.test')
    base = '/api/projects/' + PROJECT
    assert client.get(base + '/publication/dockerfile').json()['recorded'] is False
    assert client.get(base + '/builds/' + BUILD + '/dockerfile').json()['recorded'] is True
    assert client.get(base + '/builds/' + str(uuid4()) + '/dockerfile').status_code == 404
    acting[0] = other
    assert client.get(base + '/publication/dockerfile').status_code == 404
    assert client.get(base + '/builds/' + BUILD + '/dockerfile').status_code == 404
    assert calls == ['/dockerfile', '/builds/' + BUILD + '/dockerfile']
