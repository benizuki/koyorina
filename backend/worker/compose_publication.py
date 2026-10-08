"""Single-host publication controller for the Docker Compose development profile.

The management API still owns authorization and audit records. This controller
only accepts its fixed service token and talks to the local Docker daemon.
"""
import asyncio
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timezone
import hashlib
import hmac
import io
import json
import os
from pathlib import Path
import re
import tarfile
from uuid import UUID, uuid4

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from backend.domain.publication import (PublicationResources, ROOT_BASE, build_base, image_reference,
                                        version_image_reference)
from backend.domain.system_registry import RegistrySelection
from backend.worker import registry_api
from backend.worker.publication_controller import BuildInput, PruneInput, PublishInput, TenantMoveInput, unpack_source

ROOT = Path('/var/lib/koyorina-publication')
ASSETS = Path('/app/setup/publication')
NETWORK = os.environ.get('COMPOSE_PUBLICATION_NETWORK', 'koyorina-compose')
REGISTRY = os.environ.get('COMPOSE_PUBLICATION_REGISTRY', 'localhost:5000')
DEFAULT_RESOURCES = PublicationResources(cpu_request_m=100, cpu_limit_m=1000,
    memory_request_mi=256, memory_limit_mi=1024, storage_gi=5)
TERMINAL = {'succeeded', 'failed', 'cancelled'}


def now():
    return datetime.now(timezone.utc).timestamp()


def record_path(kind, identifier):
    return ROOT / f'{kind}-{UUID(str(identifier))}.json'


def read(kind, identifier):
    path = record_path(kind, identifier)
    return json.loads(path.read_text()) if path.exists() else None


def write(kind, identifier, state):
    ROOT.mkdir(parents=True, exist_ok=True)
    path = record_path(kind, identifier)
    temporary = path.with_name(path.name + '.' + uuid4().hex)
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w') as stream:
        json.dump(state, stream, ensure_ascii=False)
    os.replace(temporary, path)


async def docker(*args, data=None, timeout=120, check=True):
    process = await asyncio.create_subprocess_exec('docker', *map(str, args),
        stdin=asyncio.subprocess.PIPE if data is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(data), timeout)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        process.kill()
        with suppress(Exception):
            await process.wait()
        raise
    output = stdout.decode(errors='replace')[-2_000_000:]
    if check and process.returncode:
        raise RuntimeError(output[-1000:] or f'docker {args[0]} failed')
    return process.returncode, output


def build_context(snapshot):
    context = io.BytesIO()
    with tarfile.open(fileobj=context, mode='w') as target:
        with tarfile.open(fileobj=io.BytesIO(snapshot), mode='r:gz') as source:
            for item in source:
                target.addfile(item, source.extractfile(item))
        for filename in ('Dockerfile.generated', 'front.py', 'entrypoint.sh', 'install.py'):
            name = 'Dockerfile' if filename == 'Dockerfile.generated' else 'platform/' + filename
            data = (ASSETS / filename).read_bytes()
            info = tarfile.TarInfo(name)
            info.size, info.mode, info.mtime = len(data), 0o644, 0
            target.addfile(info, io.BytesIO(data))
    return context.getvalue()


class ComposeController:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.tasks = {}
        self.runtime_tasks = {}

    def activate_later(self, project_id, state):
        task = asyncio.create_task(self.activate(project_id, state))
        self.runtime_tasks[str(project_id)] = task
        task.add_done_callback(lambda _: self.runtime_tasks.pop(str(project_id), None))

    async def startup(self):
        ROOT.mkdir(parents=True, exist_ok=True)
        for path in ROOT.glob('build-*.json'):
            state = json.loads(path.read_text())
            if state['status'] not in TERMINAL:
                state.update(status='failed', error='公開コントローラーが再起動しました。ビルドを再実行してください。')
                write('build', state['id'], state)
        for path in ROOT.glob('published-*.json'):
            state = json.loads(path.read_text())
            if state['status'] in ('starting', 'updating'):
                state.update(status='failed', error='起動処理が中断されました。再度起動してください。')
                write('published', state['project_id'], state)
            elif state['status'] == 'running':
                code, output = await docker('inspect', self.name(state['project_id']), check=False)
                if code or not json.loads(output)[0]['State']['Running']:
                    candidate = state['current']
                    state.update(status='starting', current=None, pending=candidate, build_id=None,
                                 error=None)
                    write('published', state['project_id'], state)
                    if candidate:
                        self.activate_later(state['project_id'], state)

    async def shutdown(self):
        for task in list(self.tasks.values()) + list(self.runtime_tasks.values()):
            task.cancel()
        for task in list(self.tasks.values()) + list(self.runtime_tasks.values()):
            with suppress(asyncio.CancelledError):
                await task
        # These containers are not Compose services. Remove them before Compose
        # removes its network; the named data volumes and published intent remain.
        for path in ROOT.glob('published-*.json'):
            state = json.loads(path.read_text())
            await docker('rm', '-f', self.name(state['project_id']), check=False)

    async def registry_test(self, selection):
        if selection.kind != 'private' or (selection.host and selection.host != REGISTRY):
            return {'ok': False, 'message': 'Compose版ではローカルRegistryだけを利用できます。'}
        try:
            async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
                response = await client.get('http://registry:5000/v2/')
            return {'ok': response.status_code == 200,
                'message': 'ローカルRegistryへ接続できました。' if response.status_code == 200 else 'Registryが応答しません。'}
        except httpx.HTTPError:
            return {'ok': False, 'message': 'ローカルRegistryへ接続できません。'}

    async def submit(self, identifier, payload):
        if payload.registry and (payload.registry.kind != 'private'
                                 or payload.registry.host not in ('', REGISTRY)):
            raise HTTPException(422, 'Compose版ではローカルRegistryだけを利用できます。')
        snapshot = unpack_source(payload.source, payload.source_hash)
        async with self.lock:
            existing = read('build', identifier)
            if existing:
                return {'status': existing['status']}
            if any(path.exists() and json.loads(path.read_text())['status'] not in TERMINAL
                   for path in ROOT.glob('build-*.json')):
                raise HTTPException(409, '別のビルドが進行中です。')
            image = image_reference(REGISTRY, payload.project_id, identifier)
            state = {'id': str(identifier), 'tenant_id': str(payload.tenant_id),
                'project_id': str(payload.project_id), 'revision': payload.revision,
                'image': image, 'status': 'queued', 'digest': None, 'error': None, 'base_path': ROOT_BASE,
                'created': now(), 'dockerfile': (ASSETS / 'Dockerfile.generated').read_text()}
            write('build', identifier, state)
            task = asyncio.create_task(self.build(identifier, state, snapshot))
            self.tasks[str(identifier)] = task
            task.add_done_callback(lambda _: self.tasks.pop(str(identifier), None))
            return {'status': 'queued'}

    async def build(self, identifier, state, snapshot):
        logs = []
        try:
            state['status'] = 'building'
            write('build', identifier, state)
            revision = state['revision'] or 1
            alias = version_image_reference(REGISTRY, state['project_id'], revision)
            context = await asyncio.to_thread(build_context, snapshot)
            _, output = await docker('build', '--pull', '--build-arg',
                f'APP_BASE_PATH={build_base(state, state["project_id"])}', '--build-arg',
                f'SECURITY_UPDATE_ID={identifier}', '-t', state['image'], '-t', alias, '-',
                data=context, timeout=1200)
            logs.append(output)
            state['status'] = 'pushing'
            write('build', identifier, state)
            for tag in (state['image'], alias):
                _, output = await docker('push', tag, timeout=300)
                logs.append(output)
                if tag == state['image']:
                    digest = re.findall(r'digest:\s*(sha256:[a-f0-9]{64})', output)
                    if not digest:
                        raise RuntimeError('Push結果のdigestを確認できません。')
                    state['digest'] = digest[-1]
            state['status'] = 'succeeded'
        except asyncio.CancelledError:
            state.update(status='cancelled', error=None)
        except Exception as exc:
            logs.append(str(exc))
            state.update(status='failed', error='ビルド・Pushに失敗しました。ログを確認してください。')
        finally:
            (ROOT / f'build-{identifier}.log').write_text('\n'.join(logs)[-2_000_000:])
            write('build', identifier, state)

    async def cancel(self, identifier):
        task = self.tasks.get(str(identifier))
        if task:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        return read('build', identifier)

    async def prune_images(self, project_id, payload):
        """古いビルドのイメージを、ローカルRegistryとホストのDockerの両方から消す。"""
        removed, failed = [], []
        keep = set(payload.keep_digests)
        async with self.lock:
            for build in payload.builds:
                state = read('build', build.id)
                if state is None:
                    removed.append(str(build.id))
                    continue
                if state['project_id'] != str(project_id) or state['status'] not in TERMINAL:
                    failed.append(str(build.id))
                    continue
                # ホストからはlocalhost、Compose網の中からはサービス名で届く。
                image = 'registry:5000/' + state['image'].partition('/')[2]
                try:
                    async with httpx.AsyncClient(timeout=20, trust_env=False, follow_redirects=False) as client:
                        outcome = await registry_api.remove(client, 'http://registry:5000', {}, image,
                                                            build.digest or state.get('digest'), keep)
                except httpx.HTTPError:
                    outcome = 'failed'
                if outcome not in registry_api.SETTLED:
                    failed.append(str(build.id))
                    continue
                await docker('image', 'rm', state['image'], check=False)
                for path in (record_path('build', build.id), ROOT / f'build-{build.id}.log'):
                    path.unlink(missing_ok=True)
                removed.append(str(build.id))
        return {'removed': removed, 'failed': failed}

    @staticmethod
    def name(project_id):
        return 'koyorina-published-' + str(UUID(str(project_id)))

    @staticmethod
    def volume(project_id):
        return 'koyorina-publication-' + str(UUID(str(project_id)))

    async def run_app(self, state):
        name = self.name(state['project_id'])
        volume = self.volume(state['project_id'])
        await docker('volume', 'create', volume)
        await docker('run', '--rm', '--mount', f'type=volume,source={volume},target=/var/published',
            'busybox:1.37', 'chown', '-R', '10001:10001', '/var/published', timeout=90)
        resources = PublicationResources.model_validate(state['resources'])
        args = ['run', '-d', '--name', name, '--network', NETWORK, '--restart', 'unless-stopped',
            '--read-only', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
            '--pids-limit', '256', '--cpus', str(resources.cpu_limit_m / 1000),
            '--memory', f'{resources.memory_limit_mi}m', '--tmpfs', '/tmp:rw,nosuid,nodev,size=256m',
            '--mount', f'type=volume,source={volume},target=/var/published']
        for key, value in state['environment'].items():
            args.extend(['-e', f'{key}={value}'])
        args.append(state['image_id'])
        await docker(*args)
        for _ in range(60):
            try:
                async with httpx.AsyncClient(timeout=3, trust_env=False) as client:
                    response = await client.get(f'http://{name}:8080/__preview/health')
                if response.status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(3)
        raise RuntimeError('起動確認が時間内に完了しませんでした。')

    async def publish(self, project_id, payload):
        build = read('build', payload.build_id)
        if (not build or build['status'] != 'succeeded' or build['project_id'] != str(project_id)
                or build['tenant_id'] != str(payload.tenant_id)
                or payload.image != build['image'].rsplit(':', 1)[0] + '@' + build['digest']):
            raise HTTPException(409, '完了したこのアプリのビルドを選んでください。')
        async with self.lock:
            previous = read('published', project_id)
            if previous and previous['status'] in ('starting', 'updating', 'stopping', 'deleting'):
                raise HTTPException(409, '公開操作中です。')
            _, image_id = await docker('image', 'inspect', '--format', '{{.Id}}', build['image'])
            values = {**payload.environment, 'APP_ORIGIN': payload.app_origin,
                'APP_BASE_PATH': build_base(build, project_id), 'APP_FORWARD_SECRET': payload.forward_secret,
                'APP_SESSION_SECRET': hashlib.sha256((payload.forward_secret + ':session').encode()).hexdigest(),
                'DATABASE_URL': 'sqlite+pysqlite:////var/published/db/app.db',
                'PREVIEW_VAR': '/var/published'}
            candidate = {'project_id': str(project_id), 'tenant_id': str(payload.tenant_id),
                'build_id': str(payload.build_id), 'image_id': image_id.strip(),
                'environment': values, 'resources': (payload.resources or DEFAULT_RESOURCES).model_dump()}
            current = previous.get('current') if previous and previous['status'] == 'running' else None
            state = {'project_id': str(project_id), 'tenant_id': str(payload.tenant_id),
                'status': 'updating' if current else 'starting', 'build_id': current['build_id'] if current else None,
                'current': current, 'pending': candidate, 'error': None}
            write('published', project_id, state)
            self.activate_later(project_id, state)
            return state

    async def activate(self, project_id, state):
        previous = state['current']
        name = self.name(project_id)
        try:
            await docker('rm', '-f', name, check=False)
            await self.run_app(state['pending'])
            state.update(status='running', current=state['pending'], pending=None,
                         build_id=state['pending']['build_id'])
        except Exception as exc:
            await docker('rm', '-f', name, check=False)
            if previous:
                try:
                    await self.run_app(previous)
                    state.update(status='running', current=previous, pending=None,
                        build_id=previous['build_id'], error='起動に失敗したため直前の版へ戻しました。 ' + str(exc)[:200])
                except Exception:
                    state.update(status='failed', pending=None, error='旧版の復旧にも失敗しました。 ' + str(exc)[:200])
            else:
                state.update(status='failed', pending=None, error='アプリの起動に失敗しました。 ' + str(exc)[:200])
        write('published', project_id, state)

    async def stop(self, project_id):
        async with self.lock:
            state = read('published', project_id)
            if not state:
                return {'status': 'stopped'}
            if state['status'] in ('starting', 'updating'):
                raise HTTPException(409, '公開操作中です。')
            await docker('rm', '-f', self.name(project_id), check=False)
            state.update(status='stopped', pending=None)
            write('published', project_id, state)
            return state

    async def purge(self, project_id):
        async with self.lock:
            state = read('published', project_id)
            if state and state['status'] != 'stopped':
                raise HTTPException(409, '公開アプリを停止してください。')
            code, _ = await docker('inspect', self.name(project_id), check=False)
            if code == 0:
                raise HTTPException(409, '公開コンテナが残っています。')
            exists, _ = await docker('volume', 'inspect', self.volume(project_id), check=False)
            if exists == 0:
                removed, _ = await docker('volume', 'rm', self.volume(project_id), check=False)
                if removed:
                    raise HTTPException(503, '公開データの削除を確認できません。再試行してください。')
            record_path('published', project_id).unlink(missing_ok=True)
            return {'status': 'deleted'}


def create_controller():
    token = Path('/run/koyorina/publication_controller_token').read_text().strip()
    if len(token) < 32:
        raise ValueError('公開コントローラーの認証鍵が必要です。')
    controller = ComposeController()

    @asynccontextmanager
    async def lifespan(app):
        await controller.startup()
        try:
            yield
        finally:
            await controller.shutdown()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware('http')
    async def authenticate(request: Request, next):
        if request.url.path != '/healthz' and not hmac.compare_digest(
                request.headers.get('authorization', ''), 'Bearer ' + token):
            return JSONResponse({'detail': 'Unauthorized'}, status_code=401)
        return await next(request)

    @app.get('/healthz')
    async def health():
        return {'ok': True}

    @app.post('/registry/test')
    async def registry_test(payload: RegistrySelection):
        return await controller.registry_test(payload)

    @app.post('/builds/{build_id}', status_code=202)
    async def submit(build_id: UUID, payload: BuildInput):
        return await controller.submit(build_id, payload)

    @app.get('/builds/{build_id}')
    async def build_status(build_id: UUID):
        state = read('build', build_id)
        if not state:
            raise HTTPException(404)
        return {key: state.get(key) for key in ('status', 'digest', 'error', 'image')}

    @app.get('/dockerfile')
    async def dockerfile():
        return {'dockerfile': (ASSETS / 'Dockerfile.generated').read_text(), 'recorded': False}

    @app.get('/builds/{build_id}/dockerfile')
    async def build_dockerfile(build_id: UUID):
        state = read('build', build_id)
        if not state:
            raise HTTPException(404)
        return {'dockerfile': state['dockerfile'], 'recorded': True}

    @app.get('/builds/{build_id}/logs')
    async def logs(build_id: UUID):
        if not read('build', build_id):
            raise HTTPException(404)
        path = ROOT / f'build-{build_id}.log'
        return {'logs': path.read_text() if path.exists() else 'ビルドを準備しています。'}

    @app.delete('/builds/{build_id}')
    async def cancel(build_id: UUID):
        return await controller.cancel(build_id)

    @app.post('/projects/{project_id}/images/prune')
    async def prune(project_id: UUID, payload: PruneInput):
        return await controller.prune_images(project_id, payload)

    @app.post('/projects/{project_id}', status_code=202)
    async def publish(project_id: UUID, payload: PublishInput):
        state = await controller.publish(project_id, payload)
        return {key: state.get(key) for key in ('status', 'build_id', 'error')}

    @app.get('/projects/{project_id}')
    async def published(project_id: UUID):
        state = read('published', project_id)
        if not state:
            raise HTTPException(404)
        return {key: state.get(key) for key in ('status', 'build_id', 'error')}

    @app.get('/projects/{project_id}/resources')
    async def resources(project_id: UUID):
        state = read('published', project_id)
        return {'defaults': DEFAULT_RESOURCES.model_dump(),
            'pvc_size': f'{state["current"]["resources"]["storage_gi"]}Gi' if state and state.get('current') else None,
            'expandable': False, 'min_storage_gi': 1}

    @app.get('/projects/{project_id}/runtime')
    async def runtime(project_id: UUID):
        name = controller.name(project_id)
        code, output = await docker('inspect', name, check=False)
        if code:
            return {'available': True, 'kind': 'docker', 'deployment': {'ready': 0, 'desired': 0}, 'pods': []}
        info = json.loads(output)[0]
        running = info['State']['Running']
        return {'available': True, 'kind': 'docker',
            'deployment': {'ready': int(running), 'desired': 1},
            'pods': [{'name': name, 'phase': 'Running' if running else info['State']['Status'],
                'ready': int(running), 'containers': 1, 'restarts': info.get('RestartCount', 0),
                'node': 'Docker host', 'reason': '', 'message': ''}]}

    @app.get('/projects/{project_id}/logs')
    async def runtime_logs(project_id: UUID):
        code, output = await docker('logs', '--tail', '300', controller.name(project_id), check=False)
        if code:
            raise HTTPException(404)
        return {'lines': output.splitlines()[-300:]}

    @app.delete('/projects/{project_id}')
    async def stop(project_id: UUID):
        state = await controller.stop(project_id)
        return {key: state.get(key) for key in ('status', 'build_id', 'error')}

    @app.delete('/projects/{project_id}/data')
    async def purge(project_id: UUID):
        return await controller.purge(project_id)

    @app.post('/projects/{project_id}/tenant-move')
    async def move_tenant(project_id: UUID, payload: TenantMoveInput):
        state = read('published', project_id)
        if state and state['status'] != 'stopped':
            raise HTTPException(409, '公開アプリを停止してください。')
        for identifier in payload.build_ids:
            build = read('build', identifier)
            if build and build['project_id'] == str(project_id):
                build['tenant_id'] = str(payload.target_tenant_id)
                write('build', identifier, build)
        if state:
            state['tenant_id'] = str(payload.target_tenant_id)
            write('published', project_id, state)
        return {'status': 'moved'}

    return app
