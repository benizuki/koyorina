"""古いビルドのイメージを片付ける。公開中のもの・同じ中身のものは消さない。"""
import asyncio
import base64
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4
import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from backend.core.db import AppBuild, AppPublication, Audit, Base, Project, Tenant, User
from backend.domain.publication import retention
from backend.tests.test_publication import FakeController, PROJECT, TENANT
from backend.worker import registry_api
from backend.worker.publication_controller import PruneInput

NOW = datetime(2026, 10, 8, tzinfo=timezone.utc)


def digest(n):
    return 'sha256:' + format(n, '064x')


def build(n, status='succeeded', sha=None):
    return SimpleNamespace(id=f'b{n}', status=status, digest=sha or (digest(n) if status == 'succeeded' else None),
                           created_at=NOW + timedelta(minutes=n))


def ids(builds):
    return sorted(b.id for b in builds)


def test_keeps_the_newest_successes_the_published_one_and_running_builds():
    builds = [build(n) for n in range(1, 9)] + [build(9, 'building'), build(10, 'failed'), build(11, 'cancelled')]
    remove, protected = retention(builds, 'b1', keep=3)
    # 成功の新しい3件（b6〜b8）と公開中の b1、実行中・最近の失敗は残す。
    assert ids(remove) == ['b2', 'b3', 'b4', 'b5']
    assert protected == {digest(n) for n in (1, 6, 7, 8)}


def test_failures_are_capped_separately_so_they_never_push_out_successes():
    builds = [build(1)] + [build(n, 'failed') for n in range(2, 9)]
    remove, _ = retention(builds, None, keep=2)
    assert ids(remove) == ['b2', 'b3', 'b4', 'b5', 'b6']


def test_a_build_sharing_the_published_digest_is_removed_but_its_image_is_protected():
    builds = [build(1, sha=digest(99)), build(2), build(3, sha=digest(99))]
    remove, protected = retention(builds, 'b3', keep=1)
    # 公開中の b3 が最新の成功でもあるので、b1・b2 は外れる。b1 の行は消すが中身は b3 と同じ。
    assert ids(remove) == ['b1', 'b2']
    assert digest(99) in protected


def registry(responses, calls):
    def handler(request):
        calls.append((request.method, request.url.path, request.headers.get('authorization')))
        return responses.get((request.method, request.url.path)) or httpx.Response(500)
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


IMAGE = f'registry.test:30500/{PROJECT}:old-build'


def test_registry_deletes_by_digest_inside_the_project_repository():
    async def run():
        calls = []
        path = f'/v2/{PROJECT}/manifests/'
        client = registry({('DELETE', path + digest(1)): httpx.Response(202)}, calls)
        outcome = await registry_api.remove(client, 'https://registry.test:30500', {'Authorization': 'Basic x'},
                                            IMAGE, digest(1), set())
        assert outcome == 'deleted'
        assert calls == [('DELETE', path + digest(1), 'Basic x')]
    asyncio.run(run())


def test_registry_never_deletes_a_digest_that_must_be_kept():
    async def run():
        calls = []
        client = registry({}, calls)
        assert await registry_api.remove(client, 'http://r', {}, IMAGE, digest(1), {digest(1)}) == 'shared'
        assert calls == []
    asyncio.run(run())


def test_registry_resolves_the_tag_when_the_digest_was_never_recorded():
    async def run():
        calls = []
        path = f'/v2/{PROJECT}/manifests/'
        client = registry({('HEAD', path + 'old-build'): httpx.Response(200, headers={'Docker-Content-Digest': digest(2)}),
                           ('DELETE', path + digest(2)): httpx.Response(202)}, calls)
        assert await registry_api.remove(client, 'http://r', {}, IMAGE, None, set()) == 'deleted'
        missing = registry({('HEAD', path + 'old-build'): httpx.Response(404)}, [])
        assert await registry_api.remove(missing, 'http://r', {}, IMAGE, None, set()) == 'missing'
    asyncio.run(run())


def test_registry_reports_what_must_be_retried():
    async def run():
        path = f'/v2/{PROJECT}/manifests/' + digest(1)
        disabled = registry({('DELETE', path): httpx.Response(405)}, [])
        assert await registry_api.remove(disabled, 'http://r', {}, IMAGE, digest(1), set()) == 'failed'
        denied = registry({('DELETE', path): httpx.Response(403)}, [])
        assert await registry_api.remove(denied, 'http://r', {}, IMAGE, digest(1), set()) == 'forbidden'
    asyncio.run(run())


def test_image_references_are_split_without_trusting_extra_segments():
    assert registry_api.split_image('asia-docker.pkg.dev/p/repo/app:tag') == ('asia-docker.pkg.dev', 'p/repo/app', 'tag')
    for bad in ('nohost', 'host/../other:tag', 'host/app@sha256:x', 'host/app'):
        with pytest.raises(ValueError):
            registry_api.split_image(bad)


def test_controller_removes_images_with_the_recorded_registry_credentials(monkeypatch):
    async def run():
        c = FakeController()
        auth = base64.b64encode(b'builder:secret').decode()
        config = json.dumps({'auths': {'registry.test:30500': {'auth': auth}}})
        c.store[(False, 'secrets', 'registry-private-x')] = {'data': {'.dockerconfigjson': base64.b64encode(config.encode()).decode()}}
        registry = {'kind': 'private', 'host': 'registry.test:30500', 'http': True, 'auth_secret': 'registry-private-x'}
        builds = {}
        for name, project, status in (('old', PROJECT, 'succeeded'), ('foreign', str(uuid4()), 'succeeded'),
                                      ('running', PROJECT, 'building'), ('denied', PROJECT, 'succeeded')):
            build_id = str(uuid4())
            builds[name] = build_id
            await c.record('build-' + build_id, {'id': build_id, 'project_id': project, 'status': status,
                'image': f'registry.test:30500/{project}:{build_id}', 'registry': registry, 'registry_kind': 'private',
                'digest': digest(1), 'chunks': 0, 'cleaned': True})
        seen = []

        async def remove(client, base, headers, image, sha, keep):
            seen.append((base, headers, image, sha, keep))
            return 'forbidden' if image.endswith(builds['denied']) else 'deleted'
        monkeypatch.setattr(registry_api, 'remove', remove)
        missing = str(uuid4())
        result = await c.prune_images(PROJECT, PruneInput(builds=[{'id': i} for i in (*builds.values(), missing)],
                                                          keep_digests=[digest(7)]))
        assert sorted(result['removed']) == sorted([builds['old'], missing])
        # 別プロジェクトの記録・実行中・Registryに拒まれたものは消さず、やり直しに回す。
        assert sorted(result['failed']) == sorted([builds['foreign'], builds['running'], builds['denied']])
        assert ('http://registry.test:30500', {'Authorization': 'Basic ' + auth}) == seen[0][:2]
        assert seen[0][4] == {digest(7)}
        assert (False, 'configmaps', 'build-' + builds['old']) not in c.store
        assert (False, 'configmaps', 'build-' + builds['denied']) in c.store
    asyncio.run(run())


@pytest.fixture
def platform(monkeypatch):
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    with sessions.begin() as db:
        owner = User(email='owner@example.test', role='developer')
        db.add_all([owner, Tenant(id=TENANT, name='team')])
        db.flush()
        db.add(Project(id=PROJECT, owner_id=owner.id, tenant_id=TENANT, name='App', purpose='p',
                       audience='team', fields=[]))
        owner_id = owner.id
    sent = []

    async def call(settings, method, path, payload=None, **kwargs):
        sent.append((method, path, payload))
        if path.endswith('/images/prune'):
            return {'removed': [b['id'] for b in payload['builds']], 'failed': []}
        if path.startswith('/builds/'):
            return {'status': 'succeeded', 'digest': digest(50)}
        return None
    monkeypatch.setattr('backend.api.publication.call', call)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(sessions=sessions,
        settings=SimpleNamespace(publication_enabled=True, publication_keep_builds=2))))
    yield request, sessions, sent, owner_id
    engine.dispose()


def add_builds(sessions, owner_id, count, status='succeeded'):
    with sessions.begin() as db:
        rows = [AppBuild(id=str(uuid4()), project_id=PROJECT, tenant_id=TENANT, generation_id='g', revision=n,
                         actor_id=owner_id, source_hash='0' * 64, registry_kind='private', image=f'r/{PROJECT}:{n}',
                         digest=digest(n) if status == 'succeeded' else None, status=status,
                         created_at=NOW + timedelta(minutes=n)) for n in range(1, count + 1)]
        db.add_all(rows)
        return [row.id for row in rows]


def test_a_new_successful_build_prunes_old_ones_but_keeps_the_published_one(platform):
    from backend.api.publication import sync
    request, sessions, sent, owner_id = platform
    built = add_builds(sessions, owner_id, 5)
    with sessions.begin() as db:
        db.add(AppPublication(project_id=PROJECT, build_id=built[0], status='running'))
        db.add(AppBuild(id='new', project_id=PROJECT, tenant_id=TENANT, generation_id='g', revision=6,
                        actor_id=owner_id, source_hash='0' * 64, registry_kind='private', image=f'r/{PROJECT}:new',
                        status='pushing', created_at=NOW + timedelta(minutes=9)))
    asyncio.run(sync(request, SimpleNamespace(id=PROJECT)))
    prune = [payload for method, path, payload in sent if path.endswith('/images/prune')]
    assert len(prune) == 1
    # 新しい2件（new と b5）と公開中の b1 を残す。
    assert sorted(b['id'] for b in prune[0]['builds']) == sorted(built[1:4])
    assert set(prune[0]['keep_digests']) == {digest(1), digest(5), digest(50)}
    with sessions() as db:
        assert sorted(db.scalars(select(AppBuild.id))) == sorted([built[0], built[4], 'new'])
        assert db.scalar(select(Audit.detail).where(Audit.action == 'build.pruned')) == '3 builds'


def test_nothing_is_pruned_while_a_build_of_the_project_is_running(platform):
    from backend.api.publication import prune
    request, sessions, sent, owner_id = platform
    add_builds(sessions, owner_id, 5)
    add_builds(sessions, owner_id, 1, status='building')
    assert asyncio.run(prune(request, PROJECT)) == 0
    assert not sent


def test_deleting_the_project_removes_every_image_and_reports_leftovers(platform, monkeypatch):
    from backend.api.publication import prune
    request, sessions, sent, owner_id = platform
    built = add_builds(sessions, owner_id, 3)

    async def partial(settings, method, path, payload=None, **kwargs):
        return {'removed': built[:2], 'failed': built[2:]}
    monkeypatch.setattr('backend.api.publication.call', partial)
    assert asyncio.run(prune(request, PROJECT, everything=True)) == 1
    with sessions() as db:
        assert list(db.scalars(select(AppBuild.id))) == [built[2]]


class RegistryController(FakeController):
    """内部Registryの名前空間も持つ実行基盤。"""
    def __init__(self, deployment=None, job_result='succeeded', **kwargs):
        super().__init__(**kwargs)
        self.registry = {('deployments', 'registry'): deployment} if deployment else {}
        self.job_result = job_result
        self.created = []

    async def kube(self, method, resource, name='', body=None, group='api/v1', runtime=False,
                   query='', text=False, cluster=False, namespace=None):
        if namespace is None:
            return await super().kube(method, resource, name, body, group, runtime, query, text, cluster)
        if method == 'POST':
            self.created.append(body)
            self.registry[(resource, body['metadata']['name'])] = body
            return body
        if method == 'GET' and resource == 'jobs':
            return {'status': {self.job_result: 1}} if (resource, name) in self.registry else None
        if method == 'DELETE':
            return self.registry.pop((resource, name), None)
        return self.registry.get((resource, name))


DEPLOYMENT = {'spec': {'template': {'spec': {
    'securityContext': {'runAsNonRoot': True, 'runAsUser': 10001},
    'containers': [{'name': 'registry', 'image': 'registry:3', 'securityContext': {'readOnlyRootFilesystem': True},
                    'env': [{'name': 'REGISTRY_STORAGE_FILESYSTEM_ROOTDIRECTORY', 'value': '/var/lib/registry'}]}],
    'volumes': [{'name': 'data', 'persistentVolumeClaim': {'claimName': 'registry-data'}},
                {'name': 'auth', 'secret': {'secretName': 'registry-auth'}}]}}}}
GC_TIME = datetime(2026, 10, 8, 18, 5, tzinfo=timezone.utc)


def test_garbage_collection_runs_once_a_day_at_the_chosen_hour_without_stopping_the_registry():
    async def run():
        c = RegistryController(DEPLOYMENT)
        assert await c.collect_garbage(GC_TIME.replace(hour=17)) is None
        assert await c.collect_garbage(GC_TIME) == 'succeeded'
        job = c.created[0]
        pod = job['spec']['template']['spec']
        container = pod['containers'][0]
        assert container['command'] == ['registry', 'garbage-collect', '--delete-untagged', '/etc/distribution/config.yml']
        assert container['image'] == 'registry:3'
        # 保存領域だけを渡し、Registryの認証情報は渡さない。Registry自体は止めない（scaleしない）。
        assert pod['volumes'] == [{'name': 'data', 'persistentVolumeClaim': {'claimName': 'registry-data'}}]
        assert pod['securityContext']['runAsNonRoot'] and not pod['automountServiceAccountToken']
        assert pod['affinity']['podAffinity']['requiredDuringSchedulingIgnoredDuringExecution'][0]['topologyKey'] == 'kubernetes.io/hostname'
        assert not c.maintenance
        assert await c.collect_garbage(GC_TIME.replace(minute=40)) is None
        assert len(c.created) == 1
    asyncio.run(run())


def test_garbage_collection_waits_for_running_builds_and_blocks_new_pushes():
    async def run():
        c = RegistryController(DEPLOYMENT)
        await c.record('build-x', {'id': 'x', 'project_id': PROJECT, 'status': 'building'})
        assert await c.collect_garbage(GC_TIME) is None
        assert not c.created and not c.maintenance
        c.maintenance = True
        from fastapi import HTTPException
        from backend.tests.test_publication import source
        with pytest.raises(HTTPException) as raised:
            await c.submit(str(uuid4()), source())
        assert raised.value.status_code == 409
        with pytest.raises(HTTPException):
            await c.prune_images(PROJECT, PruneInput(builds=[]))
    asyncio.run(run())


def test_garbage_collection_is_skipped_without_an_internal_registry():
    async def run():
        c = RegistryController()
        assert await c.collect_garbage(GC_TIME) is None
        assert not c.created
    asyncio.run(run())


def test_connection_check_warns_when_old_images_cannot_be_deleted(monkeypatch):
    from backend.tests.test_publication import artifact_selection
    granted = {'artifactregistry.repositories.downloadArtifacts', 'artifactregistry.repositories.uploadArtifacts'}

    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def get(self, url, **kwargs):
            return httpx.Response(200, json={'format': 'DOCKER'})
        async def post(self, url, **kwargs):
            return httpx.Response(200, json={'permissions': sorted(granted & set(kwargs['json']['permissions']))})

    async def run():
        c = FakeController()
        async def token(registry, reader=False):
            return 'token'
        c.access_token = token
        monkeypatch.setattr('backend.worker.publication_controller.httpx.AsyncClient', Client)
        result = await c.registry_test(artifact_selection())
        # 動かせるので成功にするが、古いイメージが溜まることを伝える。
        assert result['ok'] and 'repoAdmin' in result['message']
        granted.add('artifactregistry.versions.delete')
        result = await c.registry_test(artifact_selection())
        assert result['ok'] and 'repoAdmin' not in result['message']
    asyncio.run(run())
