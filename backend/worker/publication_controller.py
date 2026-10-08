"""Durable Kubernetes build and release controller. Generated code runs only in worker pods."""
import asyncio
import base64
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timezone
import hashlib
import hmac
import io
import json
from pathlib import Path, PurePosixPath
import re
import ssl
import tarfile
from uuid import UUID, uuid4
import httpx
from fastapi import FastAPI, HTTPException, Request
from typing import Annotated
from pydantic import BaseModel, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from starlette.concurrency import run_in_threadpool
from backend.core.cluster import clean_log_line, pod_problem
from backend.worker.registry_node import CONFIG as TRANSPORT_CONFIG, LABEL as NODE_LABEL, transport_hash
from backend.domain.system_registry import RegistrySelection
from backend.core import k8s_token
from backend.domain.app_images import validate
from backend.domain.publication import image_reference, version_image_reference, published_base, DIGEST_PREFIX, ACTIVE_BUILDS, PublicationResources
from backend.domain import preview_env
from backend.worker import registry_api


def stamp():
    return datetime.now(timezone.utc).timestamp()


class ControllerSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix='publication_controller_', extra='ignore')
    token: SecretStr = Field(min_length=32)
    app_name: str = Field(default='koyorina', pattern=r'^[a-z](?:[a-z0-9-]{0,42}[a-z0-9])?$')
    registry_kind: str = 'private'
    registry_http: bool = False
    registry_host: str
    # Fixed infrastructure images, configured by operator; never accepted from a request.
    buildkit_image: str
    helper_image: str
    max_builds: int = Field(default=1, ge=1, le=8)
    build_timeout: int = Field(default=1200, ge=60, le=7200)
    startup_timeout: int = Field(default=180, ge=30, le=600)
    storage_class: str = 'local-path'
    data_storage_class: str = 'local-path'
    data_access_mode: str = 'ReadWriteOnce'
    storage_size: str = '5Gi'
    cache_size: str = '10Gi'
    node: str = ''
    pull_secret: str = ''
    registry_secret: str = ''
    registry_ca_secret: str = ''
    # ADC may be configured using an external_account credential file, never a SA key.
    gcp_service_account: str = ''
    # 内部Registryのガベージコレクションを始める時刻（UTCの時）。-1で行わない。既定は日本時間3時。
    registry_gc_hour: int = Field(default=18, ge=-1, le=23)
    registry_gc_timeout: int = Field(default=1800, ge=60, le=7200)
    assets: Path = Path('/app/setup/publication')

    @model_validator(mode='after')
    def safe(self):
        if self.registry_http and self.registry_kind != 'private':
            raise ValueError('HTTP is only supported for private registries')
        if self.registry_kind == 'unconfigured' and not self.registry_host:
            pass  # ビルド要求にはGUIで保存したRegistry設定を必須にする。
        else:
            validate(self.registry_kind, self.registry_host)
        for image in (self.buildkit_image, self.helper_image):
            if not re.search(r'@sha256:[0-9a-f]{64}$', image):
                raise ValueError('Infrastructure images require digest references')
        if self.registry_kind == 'artifact' and self.build_timeout > 3000:
            raise ValueError('Artifact builds must finish within the short-lived token lifetime (3000s)')
        if self.registry_kind == 'private' and not self.registry_secret:
            raise ValueError('Private registry requires an authentication secret')
        if self.data_access_mode not in ('ReadWriteOnce', 'ReadWriteOncePod'):
            raise ValueError('Invalid publication data access mode')
        if not self.data_storage_class:
            raise ValueError('Publication data StorageClass is required')
        for value in (self.pull_secret, self.registry_secret, self.registry_ca_secret):
            if value and not re.fullmatch(r'[a-z0-9][a-z0-9.-]{0,252}', value):
                raise ValueError('Invalid secret name')
        return self

    @property
    def build_namespace(self):
        return f'{self.app_name}-build'

    @property
    def runtime_namespace(self):
        return f'{self.app_name}-published'

    @property
    def registry_namespace(self):
        return f'{self.app_name}-registry'


class BuildInput(BaseModel):
    tenant_id: UUID
    project_id: UUID
    revision: int | None = Field(default=None, ge=1)  # older API instances omit it during rollout
    source: str = Field(max_length=8 * 1024 * 1024)
    source_hash: str = Field(pattern=r'^[a-f0-9]{64}$')
    registry: RegistrySelection | None = None


class PublishInput(BaseModel):
    tenant_id: UUID
    build_id: UUID
    image: str = Field(max_length=500)
    app_origin: str = Field(max_length=200)
    forward_secret: str = Field(min_length=32, max_length=200)
    environment: dict[str, str] = Field(default_factory=dict, max_length=50)
    resources: PublicationResources | None = None

    @model_validator(mode='after')
    def safe(self):
        preview_env.parse([{'name': k, 'value': v} for k, v in self.environment.items()])
        return self


Digest = Annotated[str, Field(pattern=r'^sha256:[0-9a-f]{64}$')]


class PruneBuild(BaseModel):
    id: UUID
    digest: Digest | None = None


class PruneInput(BaseModel):
    """消すビルドと、消してはいけないダイジェスト（残すビルドのもの）。判断は管理API側。"""
    builds: list[PruneBuild] = Field(max_length=200)
    keep_digests: list[Digest] = Field(default_factory=list, max_length=200)


class TenantMoveInput(BaseModel):
    source_tenant_id: UUID
    target_tenant_id: UUID
    build_ids: list[UUID]


def unpack_source(source, expected):
    """Revalidate archive independently at the controller's trust boundary."""
    try:
        raw = base64.b64decode(source, validate=True)
        if len(raw) > 6 * 1024 * 1024:
            raise ValueError()
        files = []
        with tarfile.open(fileobj=io.BytesIO(raw), mode='r:gz') as archive:
            for info in archive:
                path = PurePosixPath(info.name)
                if (len(files) >= 100 or not info.isfile() or info.size > 200000
                        or '..' in path.parts or path.is_absolute()
                        or any(p.startswith('.') for p in path.parts)
                        or (path.parts[0] not in {'backend', 'frontend'} and info.name != 'pyproject.toml')
                        or path.parts[0] in {'data', 'var'}):
                    raise ValueError()
                files.append((info.name, archive.extractfile(info).read().decode()))
        if len({name for name, _ in files}) != len(files) or sum(len(c.encode()) for _, c in files) > 5 * 1024 * 1024:
            raise ValueError()
        sha = hashlib.sha256()
        for name, content in sorted(files):
            sha.update(name.encode() + b'\0' + content.encode() + b'\0')
        if not hmac.compare_digest(sha.hexdigest(), expected):
            raise ValueError()
        return raw
    except (ValueError, OSError, tarfile.TarError, UnicodeError):
        raise HTTPException(422, 'Invalid source snapshot') from None


class Controller:
    def __init__(self, settings):
        self.settings = settings
        self.lock = asyncio.Lock()
        self.pull_refreshed = {}
        self.transport_lock = asyncio.Lock()
        # 内部Registryのガベージコレクション中。push と削除を受け付けない（壊れるため）。
        self.maintenance = False

    async def kube(self, method, resource, name='', body=None, group='api/v1', runtime=False, query='', text=False, cluster=False,
                   namespace=None):
        namespace = namespace or (self.settings.runtime_namespace if runtime else self.settings.build_namespace)
        token = Path('/var/run/secrets/kubernetes.io/serviceaccount/token').read_text().strip()
        context = ssl.create_default_context(cafile='/var/run/secrets/kubernetes.io/serviceaccount/ca.crt')
        base = f'https://kubernetes.default.svc/{group}'
        url = f'{base}/{resource}' if cluster else f'{base}/namespaces/{namespace}/{resource}'
        if name:
            url += '/' + name
        headers = {'Authorization': 'Bearer ' + token}
        if method == 'PATCH':
            headers['Content-Type'] = 'application/apply-patch+yaml'
            query = query or '?fieldManager=koyorina-publication&force=true'
        async with httpx.AsyncClient(verify=context, timeout=20, trust_env=False) as client:
            response = await client.request(method, url + query, headers=headers, json=body)
        if response.status_code == 404:
            return None
        if not response.is_success:
            raise HTTPException(503, 'Publication Kubernetes operation failed')
        return response.text if text else response.json() if response.content else {}

    async def access_token(self, registry, reader=False):
        identity = 'publication-puller' if reader else 'publication-controller'
        response = await self.kube('POST', 'serviceaccounts', identity + '/token', body={
            'apiVersion': 'authentication.k8s.io/v1', 'kind': 'TokenRequest',
            'spec': {'audiences': [registry.audience()], 'expirationSeconds': 3600}})
        token = response['status']['token']
        def refresh():
            from google.auth import identity_pool
            from google.auth.transport.requests import Request
            class Supplier(identity_pool.SubjectTokenSupplier):
                def get_subject_token(self, context, request):
                    return token
            credentials = k8s_token.credentials(registry.credential_config(reader), Supplier())
            credentials.refresh(Request())
            return credentials.token
        return await run_in_threadpool(refresh)

    async def docker_secret(self, registry, name, runtime=False, reader=False):
        if registry.kind == 'private':
            auth = base64.b64encode((registry.username + ':' + registry.password).encode()).decode()
        else:
            token = await self.access_token(registry, reader)
            auth = base64.b64encode(('oauth2accesstoken:' + token).encode()).decode()
        config = json.dumps({'auths': {registry.host.split('/')[0]: {'auth': auth}}})
        await self.kube('PATCH', 'secrets', name, runtime=runtime, body={
            'apiVersion': 'v1', 'kind': 'Secret', 'metadata': {'name': name},
            'type': 'kubernetes.io/dockerconfigjson',
            'data': {'.dockerconfigjson': base64.b64encode(config.encode()).decode()}})

    async def ensure_pull(self, registry):
        if registry.get('auth_secret'):
            name = registry['auth_secret']
            source = await self.kube('GET', 'secrets', name)
            if not source:
                raise HTTPException(503, 'Registry credentials are unavailable')
            await self.kube('PATCH', 'secrets', name, runtime=True, body={
                'apiVersion': 'v1', 'kind': 'Secret', 'metadata': {'name': name},
                'type': 'kubernetes.io/dockerconfigjson', 'data': source['data']})
            return name
        registry = RegistrySelection.model_validate(registry)
        name = 'registry-pull-' + hashlib.sha256(json.dumps(registry.model_dump(), sort_keys=True).encode()).hexdigest()[:24]
        if stamp() - self.pull_refreshed.get(name, 0) >= 1800:
            await self.docker_secret(registry, name, runtime=True, reader=True)
            self.pull_refreshed[name] = stamp()
        return name

    async def ensure_transport(self, registry=None):
        async with self.transport_lock:
            await self.sync_transport(registry)

    async def sync_transport(self, registry=None):
        kind = registry.get('kind') if registry else self.settings.registry_kind
        if kind != 'private':
            return
        host = (registry or {}).get('host') or self.settings.registry_host
        http = (registry or {}).get('http', self.settings.registry_http)
        if host != self.settings.registry_host:
            raise HTTPException(422, 'ノードのPull設定を変更できるのはAnsibleで配備した内部Registryです。接続先を変更する場合はAnsibleも更新してください。')
        desired = {'host': host, 'http': http}
        await self.kube('PATCH', 'configmaps', TRANSPORT_CONFIG, body={'apiVersion': 'v1', 'kind': 'ConfigMap',
            'metadata': {'name': TRANSPORT_CONFIG}, 'data': {'transport': json.dumps(desired)}})
        for _ in range(15):
            daemon = await self.kube('GET', 'daemonsets', 'registry-node', group='apis/apps/v1')
            if not daemon:
                raise HTTPException(422, 'Registryのノード同期基盤が未配備です。新しい本体イメージでAnsibleのsite.ymlを再適用してください。')
            pods = await self.kube('GET', 'pods', query='?labelSelector=app%3D' + NODE_LABEL)
            nodes = {pod.get('spec', {}).get('nodeName') for pod in (pods or {}).get('items', [])
                     if not pod.get('metadata', {}).get('deletionTimestamp') and pod.get('spec', {}).get('nodeName')}
            leases = await self.kube('GET', 'leases', group='apis/coordination.k8s.io/v1',
                query='?labelSelector=app%3D' + NODE_LABEL)
            applied = set()
            for lease in (leases or {}).get('items', []):
                annotations = lease.get('metadata', {}).get('annotations', {})
                renewed = lease.get('spec', {}).get('renewTime')
                if renewed and annotations.get('transport-hash') == transport_hash(host, http):
                    when = datetime.fromisoformat(renewed.replace('Z', '+00:00')).timestamp()
                    if stamp() - when < 15:
                        applied.add(annotations.get('node'))
            count = daemon.get('status', {}).get('desiredNumberScheduled', 0)
            if count > 0 and len(nodes) == count and nodes <= applied:
                return
            await asyncio.sleep(2)
        raise HTTPException(422, '内部RegistryのHTTP/TLS設定を全ノードへ反映できません。接続・公開環境のregistry-node Podの状態とログを確認してください。')

    async def registry_test(self, registry):
        try:
            if registry.kind == 'private':
                if self.settings.registry_kind != 'private':
                    return {'ok': False, 'message': '内部RegistryをAnsibleで構成してください。'}
                if not registry.host:
                    await self.ensure_transport(registry.model_dump(exclude={'password'}))
                    return {'ok': True, 'message': 'Ansibleの内部Registry設定を使用し、全ノードへのHTTP/TLS設定の反映を確認しました。'}
                trust = ssl.create_default_context()
                if not registry.http and self.settings.registry_ca_secret:
                    ca = await self.kube('GET', 'secrets', self.settings.registry_ca_secret)
                    if ca:
                        trust.load_verify_locations(cadata=base64.b64decode(ca['data']['ca.crt']).decode())
                async with httpx.AsyncClient(verify=trust, timeout=20, trust_env=False, follow_redirects=False) as client:
                    response = await client.get(('http://' if registry.http else 'https://') + registry.host + '/v2/',
                                                auth=(registry.username, registry.password))
                if response.is_success:
                    await self.ensure_transport(registry.model_dump(exclude={'password'}))
                return {'ok': response.is_success, 'message': '内部Registryへの認証と、全ノードへのHTTP/TLS設定の反映を確認しました。' if response.is_success else '内部Registryの接続先と認証情報を確認してください。'}
            # Read repository metadata with both identities; writer IAM must be bound per repository.
            cleanup = True
            region_host, project, repository = registry.host.split('/')
            region = region_host.removesuffix('-docker.pkg.dev')
            url = f'https://artifactregistry.googleapis.com/v1/projects/{project}/locations/{region}/repositories/{repository}'
            for reader in (False, True):
                token = await self.access_token(registry, reader)
                async with httpx.AsyncClient(timeout=20, trust_env=False, follow_redirects=False) as client:
                    response = await client.get(url, headers={'Authorization': 'Bearer ' + token})
                    if not response.is_success:
                        return {'ok': False, 'message': 'WIF認証またはリポジトリへのアクセスを確認できません。IAM設定を確認してください。'}
                    permissions = ['artifactregistry.repositories.downloadArtifacts']
                    if not reader:
                        permissions += ['artifactregistry.repositories.uploadArtifacts', 'artifactregistry.versions.delete']
                    response = await client.post(url + ':testIamPermissions', headers={'Authorization': 'Bearer ' + token},
                                                 json={'permissions': permissions})
                    granted = set(response.json().get('permissions', [])) if response.is_success else set()
                    if not set(permissions[:2]).issubset(granted):
                        return {'ok': False, 'message': 'Push用のwriter権限またはPull用のreader権限が不足しています。'}
                    if not reader and 'artifactregistry.versions.delete' not in granted:
                        cleanup = False
            message = 'Push用・Pull用のWIF認証とリポジトリへのアクセスを確認しました。実際のPushはビルド時に確認します。'
            if not cleanup:
                # 動かすことはできるので失敗にはしない。ただし古いイメージが溜まり続ける。
                message += ('ただしPush用のサービスアカウントに削除権限（artifactregistry.versions.delete）が無いため、'
                            '古いビルドのイメージを消せません。roles/artifactregistry.repoAdmin を付与してください。')
            return {'ok': True, 'message': message}
        except HTTPException as exc:
            if exc.status_code == 422:
                return {'ok': False, 'message': exc.detail}
            return {'ok': False, 'message': 'ノードのRegistry設定を確認できません。公開基盤の配備状態を確認してください。'}
        except Exception:
            return {'ok': False, 'message': '内部Registryの接続・認証に失敗しました。DNS・HTTP/TLS・通信許可を確認してください。' if registry.kind == 'private' else 'WIF認証に失敗しました。公開鍵・audience・IAM権限と外部通信を確認してください。'}

    async def record(self, name, state=None):
        if state is None:
            item = await self.kube('GET', 'configmaps', name)
            return json.loads(item['data']['state']) if item else None
        await self.kube('PATCH', 'configmaps', name, body={'apiVersion': 'v1', 'kind': 'ConfigMap',
            'metadata': {'name': name, 'namespace': self.settings.build_namespace,
                'labels': {'koyorina-record': 'publication'}}, 'data': {'state': json.dumps(state)}})
        return state

    async def claim(self, name, size, runtime=False):
        namespace = self.settings.runtime_namespace if runtime else self.settings.build_namespace
        existing = await self.kube('GET', 'persistentvolumeclaims', name, runtime=runtime)
        if existing is None:
            if runtime:
                storage_class = await self.kube('GET', 'storageclasses',
                    self.settings.data_storage_class, group='apis/storage.k8s.io/v1', cluster=True)
                minimum = 10 if storage_class and storage_class.get('provisioner') == 'pd.csi.storage.gke.io' else 1
                if int(size[:-2]) < minimum:
                    raise HTTPException(422, f'公開PVCは{minimum}Gi以上を指定してください。')
            await self.kube('POST', 'persistentvolumeclaims', runtime=runtime, body={
                'apiVersion': 'v1', 'kind': 'PersistentVolumeClaim', 'metadata': {'name': name},
                'spec': {'accessModes': [self.settings.data_access_mode if runtime else 'ReadWriteOnce'],
                    'storageClassName': self.settings.data_storage_class if runtime else self.settings.storage_class,
                    'resources': {'requests': {'storage': size}}}})
        elif runtime:
            current = existing['spec']['resources']['requests']['storage']
            current_gi = int(current[:-2]) if re.fullmatch(r'[1-9][0-9]*Gi', current) else None
            requested_gi = int(size[:-2])
            if current_gi is None:
                raise HTTPException(422, '公開PVCの現在の容量を解釈できません。管理者に確認してください。')
            if requested_gi < current_gi:
                raise HTTPException(422, '公開PVCの容量は縮小できません。現在の容量以上を指定してください。')
            if requested_gi > current_gi:
                storage_class = await self.kube('GET', 'storageclasses',
                    existing['spec']['storageClassName'], group='apis/storage.k8s.io/v1', cluster=True)
                if not storage_class or not storage_class.get('allowVolumeExpansion'):
                    raise HTTPException(422, '公開PVCのStorageClassは容量拡張に対応していません。')
                await self.kube('PATCH', 'persistentvolumeclaims', name, runtime=True, body={
                    'apiVersion': 'v1', 'kind': 'PersistentVolumeClaim', 'metadata': {'name': name},
                    'spec': {'resources': {'requests': {'storage': size}}}})

    def default_resources(self):
        match = re.fullmatch(r'([1-9][0-9]*)Gi', self.settings.storage_size)
        if not match:
            raise HTTPException(503, 'Invalid publication storage default')
        return PublicationResources(cpu_request_m=100, cpu_limit_m=1000,
            memory_request_mi=256, memory_limit_mi=1024, storage_gi=int(match.group(1)))

    async def resource_info(self, project_id):
        claim = await self.kube('GET', 'persistentvolumeclaims',
            'published-' + str(project_id) + '-data', runtime=True)
        class_name = claim['spec']['storageClassName'] if claim else self.settings.data_storage_class
        storage_class = await self.kube('GET', 'storageclasses',
            class_name, group='apis/storage.k8s.io/v1', cluster=True)
        minimum = 10 if storage_class and storage_class.get('provisioner') == 'pd.csi.storage.gke.io' else 1
        expandable = bool(storage_class and storage_class.get('allowVolumeExpansion')) if claim else None
        return {'defaults': self.default_resources().model_dump(),
                'pvc_size': claim['spec']['resources']['requests']['storage'] if claim else None,
                'expandable': expandable, 'min_storage_gi': minimum}

    async def submit(self, build_id, payload):
        raw = await run_in_threadpool(unpack_source, payload.source, payload.source_hash)
        async with self.lock:
            if self.maintenance:
                raise HTTPException(409, 'Registry maintenance is running')
            name = 'build-' + str(build_id)
            existing = await self.record(name)
            if existing:
                if (existing['source_hash'] != payload.source_hash or existing['project_id'] != str(payload.project_id)
                        or (existing.get('revision') is not None and payload.revision is not None
                            and existing['revision'] != payload.revision)):
                    raise HTTPException(409, 'Build already exists')
                if existing.get('snapshot_ready') or existing['status'] not in ACTIVE_BUILDS:
                    return existing
            encoded = base64.b64encode(raw).decode()
            chunks = [encoded[i:i + 600000] for i in range(0, len(encoded), 600000)]
            if not payload.registry and self.settings.registry_kind == 'unconfigured':
                raise HTTPException(409, 'システム設定でイメージの保存先を設定してください。')
            if payload.registry and payload.registry.kind == 'private' and self.settings.registry_kind != 'private':
                raise HTTPException(409, 'Private registry is not provisioned')
            registry = payload.registry
            host = registry.host if registry and registry.host else self.settings.registry_host
            registry_state = registry.model_dump(exclude={'password'}) if registry else None
            if registry_state and registry.kind == 'private' and not registry.host:
                registry_state['host'] = self.settings.registry_host
            if registry and registry.kind == 'private' and registry.host:
                if not registry.password:
                    raise HTTPException(422, 'Registry password is required')
                fingerprint = hashlib.sha256(json.dumps(registry.model_dump(), sort_keys=True).encode()).hexdigest()[:24]
                auth_name = 'registry-private-' + fingerprint
                await self.docker_secret(registry, auth_name)
                registry_state['auth_secret'] = auth_name
            state = existing or {'id': str(build_id), 'project_id': str(payload.project_id),
                'tenant_id': str(payload.tenant_id), 'source_hash': payload.source_hash,
                'revision': payload.revision,
                'image': image_reference(host, payload.project_id, build_id),
                'version_image': (version_image_reference(host, payload.project_id, payload.revision)
                    if payload.revision else None),
                'registry': registry_state,
                'registry_kind': registry.kind if registry else self.settings.registry_kind,
                'chunks': len(chunks), 'status': 'queued', 'created': stamp(), 'snapshot_ready': False,
                'dockerfile': await asyncio.to_thread((self.settings.assets / 'Dockerfile.generated').read_text)}
            await self.record(name, state)
            for i, chunk in enumerate(chunks):
                await self.kube('PATCH', 'configmaps', f'{name}-source-{i}', body={
                    'apiVersion': 'v1', 'kind': 'ConfigMap', 'metadata': {'name': f'{name}-source-{i}'},
                    'data': {'chunk': chunk}})
            state['snapshot_ready'] = True
            return await self.record(name, state)

    async def credentials(self, name, registry=None):
        if registry and registry.get('auth_secret'):
            return registry['auth_secret']
        if registry and registry.get('kind') == 'artifact':
            await self.docker_secret(RegistrySelection.model_validate(registry), name)
            return name
        if self.settings.registry_kind == 'private':
            return self.settings.registry_secret
        def refresh():
            import google.auth
            from google.auth.transport.requests import Request
            credentials, _ = google.auth.default(scopes=['https://www.googleapis.com/auth/cloud-platform'])
            credentials.refresh(Request())
            return credentials.token
        token = await run_in_threadpool(refresh)
        auth = base64.b64encode(('oauth2accesstoken:' + token).encode()).decode()
        host = self.settings.registry_host.split('/')[0]
        config = json.dumps({'auths': {host: {'auth': auth}}})
        await self.kube('PATCH', 'secrets', name, body={'apiVersion': 'v1', 'kind': 'Secret',
            'metadata': {'name': name}, 'type': 'kubernetes.io/dockerconfigjson',
            'data': {'.dockerconfigjson': base64.b64encode(config.encode()).decode()}})
        return name

    async def start_build(self, state):
        s = self.settings
        registry = state.get('registry')
        if registry and registry['kind'] == 'artifact':
            s = s.model_copy(update={'registry_kind': 'artifact', 'registry_host': registry['host'], 'registry_http': False, 'registry_ca_secret': '', 'build_timeout': min(s.build_timeout, 3000)})
        if registry and registry['kind'] == 'private' and registry.get('host'):
            s = s.model_copy(update={'registry_kind': 'private', 'registry_host': registry['host'], 'registry_http': registry.get('http', False)})
        name = 'build-' + state['id']
        cache = 'cache-' + state['tenant_id']
        await self.claim(cache, s.cache_size)
        credential = await self.credentials(name + '-auth', state.get('registry'))
        platform = {'Dockerfile': state.get('dockerfile') or (s.assets / 'Dockerfile.generated').read_text(),
            'front.py': (s.assets / 'front.py').read_text(),
            'entrypoint.sh': (s.assets / 'entrypoint.sh').read_text(),
            'install.py': (s.assets / 'install.py').read_text()}
        await self.kube('PATCH', 'configmaps', name + '-platform', body={'apiVersion': 'v1',
            'kind': 'ConfigMap', 'metadata': {'name': name + '-platform'}, 'data': platform})
        sources = [{'name': f'source-{i}', 'configMap': {'name': f'{name}-source-{i}'}}
                   for i in range(state['chunks'])]
        source_mounts = [{'name': f'source-{i}', 'mountPath': f'/chunks/{i}', 'readOnly': True}
                         for i in range(state['chunks'])]
        prepare = """import base64, pathlib, tarfile, io, shutil
raw = base64.b64decode(''.join(p.read_text() for p in sorted(pathlib.Path('/chunks').glob('*/chunk'), key=lambda p:int(p.parent.name))))
with tarfile.open(fileobj=io.BytesIO(raw), mode='r:gz') as t:
 t.extractall('/source', filter='data')
shutil.copy('/platform/Dockerfile', '/source/Dockerfile')
shutil.copytree('/platform', '/source/platform', dirs_exist_ok=True)
"""
        build_script = r'''set -eu
if [ -f /registry-ca/ca.crt ]; then
 cat /etc/ssl/certs/ca-certificates.crt /registry-ca/ca.crt > /tmp/registry-ca-bundle.crt
fi
printf '[registry."%s"]\n  http = %s\n' "$REGISTRY_ENDPOINT" "$REGISTRY_HTTP" > /tmp/buildkitd.toml
buildctl-daemonless.sh build --frontend dockerfile.v0 --local context=/source --local dockerfile=/source \
 --opt image-resolve-mode=pull \
 --opt build-arg:APP_BASE_PATH="$APP_BASE_PATH" \
 --opt build-arg:SECURITY_UPDATE_ID="$BUILD_ID" \
 --import-cache type=registry,ref="$CACHE_IMAGE" --export-cache type=registry,ref="$CACHE_IMAGE",mode=max,registry.insecure="$REGISTRY_HTTP" \
 --output "type=image,\"name=$OUTPUT_NAMES\",push=true,registry.insecure=$REGISTRY_HTTP" --metadata-file /tmp/result.json
printf 'KOYORINA_IMAGE_DIGEST='
sed -n 's/.*"containerimage.digest": "\(sha256:[a-f0-9]*\)".*/\1/p' /tmp/result.json
'''
        mounts = [{'name': 'source', 'mountPath': '/source', 'readOnly': True},
            {'name': 'auth', 'mountPath': '/home/user/.docker', 'readOnly': True},
            {'name': 'cache', 'mountPath': '/home/user/.local/share/buildkit'},
            {'name': 'tmp', 'mountPath': '/tmp'}]
        volumes = [*sources, {'name': 'source', 'emptyDir': {'sizeLimit': '32Mi'}},
            {'name': 'platform', 'configMap': {'name': name + '-platform'}},
            {'name': 'auth', 'secret': {'secretName': credential,
                'items': [{'key': '.dockerconfigjson', 'path': 'config.json'}]}},
            {'name': 'cache', 'persistentVolumeClaim': {'claimName': cache}},
            {'name': 'tmp', 'emptyDir': {'sizeLimit': '2Gi'}}]
        # Custom CA extends system trust for BuildKit registry clients.
        if s.registry_ca_secret:
            volumes.append({'name': 'registry-ca', 'secret': {'secretName': s.registry_ca_secret}})
            mounts.append({'name': 'registry-ca', 'mountPath': '/registry-ca', 'readOnly': True})
        env = [{'name': k, 'value': v} for k, v in {
            'OUTPUT_NAMES': ','.join(filter(None, (state['image'], state.get('version_image')))),
            'CACHE_IMAGE': f'{s.registry_host}/cache-{state["tenant_id"]}:buildkit',
            'APP_BASE_PATH': published_base(state['project_id']), 'BUILDKITD_FLAGS': '--oci-worker-no-process-sandbox --config /tmp/buildkitd.toml',
            'BUILD_ID': state['id'],
            'REGISTRY_ENDPOINT': s.registry_host.split('/')[0], 'REGISTRY_HTTP': str(s.registry_http).lower(),
            'DOCKER_CONFIG': '/home/user/.docker',
            **({'SSL_CERT_FILE': '/tmp/registry-ca-bundle.crt'} if s.registry_ca_secret else {})}.items()]
        pod = {'restartPolicy': 'Never', 'automountServiceAccountToken': False,
            'enableServiceLinks': False,
            'securityContext': {'runAsNonRoot': True, 'runAsUser': 1000, 'runAsGroup': 1000, 'fsGroup': 1000},
            'initContainers': [{'name': 'prepare', 'image': s.helper_image,
                'command': ['python', '-c', prepare],
                'securityContext': {'allowPrivilegeEscalation': False, 'capabilities': {'drop': ['ALL']},
                    'seccompProfile': {'type': 'RuntimeDefault'}},
                'volumeMounts': [*source_mounts, {'name': 'source', 'mountPath': '/source'},
                    {'name': 'platform', 'mountPath': '/platform', 'readOnly': True}]}],
            'containers': [{'name': 'build', 'image': s.buildkit_image,
                'command': ['sh', '-c', build_script], 'env': env,
                'securityContext': {'seccompProfile': {'type': 'Unconfined'},
                    'appArmorProfile': {'type': 'Unconfined'}},
                'resources': {'requests': {'cpu': '250m', 'memory': '1Gi'},
                    'limits': {'cpu': '2', 'memory': '3Gi'}}, 'volumeMounts': mounts}], 'volumes': volumes}
        pod['tolerations'] = [{'key': 'workload', 'operator': 'Equal', 'value': s.app_name + '-agent', 'effect': 'NoSchedule'}]
        if s.node:
            pod['nodeSelector'] = {'kubernetes.io/hostname': s.node}
        if s.pull_secret:
            pod['imagePullSecrets'] = [{'name': s.pull_secret}]
        await self.kube('PATCH', 'jobs', name, group='apis/batch/v1', body={
            'apiVersion': 'batch/v1', 'kind': 'Job', 'metadata': {'name': name},
            'spec': {'backoffLimit': 0, 'activeDeadlineSeconds': s.build_timeout,
                'template': {'metadata': {'labels': {'koyorina-build': state['id']}}, 'spec': pod}}})
        state.update(status='building', started=stamp(), build_timeout=s.build_timeout)
        await self.record(name, state)

    async def build_dockerfile(self, state=None):
        if state is None:
            return {'dockerfile': await asyncio.to_thread((self.settings.assets / 'Dockerfile.generated').read_text), 'recorded': False}
        if state.get('dockerfile'):
            return {'dockerfile': state['dockerfile'], 'recorded': True}
        platform = await self.kube('GET', 'configmaps', 'build-' + state['id'] + '-platform')
        recipe = (platform or {}).get('data', {}).get('Dockerfile')
        return {'dockerfile': recipe or '', 'recorded': bool(recipe)}

    async def build_logs(self, state):
        pods = await self.kube('GET', 'pods', query='?labelSelector=koyorina-build%3D' + state['id'])
        items = (pods or {}).get('items', [])
        if not items:
            return state.get('logs', '')
        containers = items[0].get('status', {}).get('containerStatuses', [])
        if not any(c.get('state', {}).get('running') or c.get('state', {}).get('terminated') for c in containers):
            return state.get('logs', '')
        logs = await self.kube('GET', 'pods', items[0]['metadata']['name'] + '/log',
            query='?container=build&tailLines=300&limitBytes=40000', text=True)
        return '\n'.join(clean_log_line(line) for line in (logs or '').splitlines())

    async def tick_build(self, state):
        name = 'build-' + state['id']
        job = await self.kube('GET', 'jobs', name, group='apis/batch/v1')
        if not job:
            # A job may exist before its state write. Never duplicate a persisted running job.
            if state['status'] == 'queued':
                await self.start_build(state)
                return
            state.update(status='failed', error='ビルドジョブが失われました。再実行してください。')
        else:
            if 'started' not in state:
                start = job.get('status', {}).get('startTime')
                state['started'] = datetime.fromisoformat(start.replace('Z', '+00:00')).timestamp() if start else stamp()
            state['logs'] = await self.build_logs(state)
            status = job.get('status', {})
            if status.get('succeeded'):
                matches = re.findall(re.escape(DIGEST_PREFIX) + r'(sha256:[0-9a-f]{64})', state['logs'])
                if matches:
                    state.update(status='succeeded', digest=matches[-1], error=None)
                else:
                    state.update(status='failed', error='Push結果のdigestを確認できません。')
            elif status.get('failed') or stamp() - state.get('started', state['created']) > state.get('build_timeout', self.settings.build_timeout):
                state.update(status='failed', error='ビルド・Pushが失敗したか、時間上限を超えました。ログを確認してください。')
            else:
                state['status'] = 'pushing' if 'pushing layers' in state['logs'] else 'building'
        await self.record(name, state)
        if state['status'] not in ACTIVE_BUILDS:
            await self.cleanup_build(state)

    async def cleanup_build(self, state):
        name = 'build-' + state['id']
        await self.kube('DELETE', 'jobs', name, group='apis/batch/v1', body={'propagationPolicy': 'Foreground'})
        for suffix in [f'-source-{i}' for i in range(state['chunks'])] + ['-platform']:
            await self.kube('DELETE', 'configmaps', name + suffix)
        if state.get('registry_kind', self.settings.registry_kind) == 'artifact':
            await self.kube('DELETE', 'secrets', name + '-auth')
        state['cleaned'] = True
        await self.record(name, state)

    async def cancel(self, build_id):
        async with self.lock:
            state = await self.record('build-' + str(build_id))
            if state and state['status'] in ACTIVE_BUILDS:
                state.update(status='cancelled', error=None)
                await self.record('build-' + str(build_id), state)
                await self.cleanup_build(state)
            return state

    async def registry_trust(self, http):
        trust = ssl.create_default_context()
        if not http and self.settings.registry_ca_secret:
            ca = await self.kube('GET', 'secrets', self.settings.registry_ca_secret)
            if ca:
                trust.load_verify_locations(cadata=base64.b64decode(ca['data']['ca.crt']).decode())
        return trust

    async def remove_image(self, state, digest, keep):
        """ビルド時に記録した保存先・認証情報で、そのビルドのイメージを消す。"""
        registry = state.get('registry') or {}
        kind = registry.get('kind') or state.get('registry_kind') or self.settings.registry_kind
        endpoint, _, _ = registry_api.split_image(state['image'])
        trust = True
        if kind == 'private':
            http = registry.get('http', self.settings.registry_http)
            secret = await self.kube('GET', 'secrets', registry.get('auth_secret') or self.settings.registry_secret)
            if not secret:
                return 'failed'
            auths = json.loads(base64.b64decode(secret['data']['.dockerconfigjson']))['auths']
            headers = {'Authorization': 'Basic ' + auths[endpoint]['auth']}
            base = ('http://' if http else 'https://') + endpoint
            trust = await self.registry_trust(http)
        elif kind == 'artifact':
            if registry:
                token = await self.access_token(RegistrySelection.model_validate(registry))
            else:
                def refresh():
                    import google.auth
                    from google.auth.transport.requests import Request
                    credentials, _ = google.auth.default(scopes=['https://www.googleapis.com/auth/cloud-platform'])
                    credentials.refresh(Request())
                    return credentials.token
                token = await run_in_threadpool(refresh)
            headers = {'Authorization': 'Basic ' + base64.b64encode(('oauth2accesstoken:' + token).encode()).decode()}
            base = 'https://' + endpoint
        else:
            return 'failed'
        async with httpx.AsyncClient(verify=trust, timeout=20, trust_env=False, follow_redirects=False) as client:
            return await registry_api.remove(client, base, headers, state['image'], digest, keep)

    async def prune_images(self, project_id, payload):
        """古いビルドのイメージと記録を消す。ビルド・公開と同じロックで、push と重ねない。"""
        removed, failed = [], []
        keep = set(payload.keep_digests)
        async with self.lock:
            if self.maintenance:
                raise HTTPException(409, 'Registry maintenance is running')
            for build in payload.builds:
                name = 'build-' + str(build.id)
                state = await self.record(name)
                if state is None:
                    # 記録が無い＝片付け済みか、実行基盤に届かなかったビルド。消すものは無い。
                    removed.append(str(build.id))
                    continue
                if state['project_id'] != str(project_id) or state['status'] in ACTIVE_BUILDS:
                    failed.append(str(build.id))
                    continue
                try:
                    outcome = await self.remove_image(state, build.digest or state.get('digest'), keep)
                except Exception:
                    outcome = 'failed'
                if outcome not in registry_api.SETTLED:
                    failed.append(str(build.id))
                    continue
                if not state.get('cleaned'):
                    await self.cleanup_build(state)
                await self.kube('DELETE', 'configmaps', name)
                removed.append(str(build.id))
        return {'removed': removed, 'failed': failed}

    def gc_job(self, name, deployment):
        """Registryと同じイメージ・保存領域・権限で garbage-collect を1回だけ走らせるJob。

        Registryは止めない。GCが壊すのは同時に書き込まれたときだけで、書き込むのは
        このコントローラーが管理するビルドと削除に限られる（maintenance で止めている）。
        pull（公開アプリの起動）はそのまま続けられる。

        --delete-untagged はタグの無いマニフェスト（上書きされたビルドキャッシュ）も消す。
        ビルドは単一のマニフェストで push しており、インデックスの子を巻き込むことはない。
        """
        pod = deployment['spec']['template']['spec']
        registry = next(c for c in pod['containers'] if c['name'] == 'registry')
        data = next(v for v in pod['volumes'] if v['name'] == 'data')
        root = next((e['value'] for e in registry.get('env', [])
                     if e['name'] == 'REGISTRY_STORAGE_FILESYSTEM_ROOTDIRECTORY'), '/var/lib/registry')
        return {'apiVersion': 'batch/v1', 'kind': 'Job', 'metadata': {'name': name, 'labels': {'app': 'registry-gc'}},
            'spec': {'backoffLimit': 0, 'activeDeadlineSeconds': self.settings.registry_gc_timeout,
                'ttlSecondsAfterFinished': 86400,
                'template': {'metadata': {'labels': {'app': 'registry-gc'}}, 'spec': {
                    'restartPolicy': 'Never', 'automountServiceAccountToken': False, 'enableServiceLinks': False,
                    'securityContext': pod.get('securityContext', {}),
                    # RWOの保存領域は、Registryと同じノードでしか同時に使えない。
                    'affinity': {'podAffinity': {'requiredDuringSchedulingIgnoredDuringExecution': [{
                        'labelSelector': {'matchLabels': {'app': 'registry'}},
                        'topologyKey': 'kubernetes.io/hostname'}]}},
                    'tolerations': pod.get('tolerations', []),
                    'containers': [{'name': 'gc', 'image': registry['image'],
                        'command': ['registry', 'garbage-collect', '--delete-untagged', '/etc/distribution/config.yml'],
                        'env': [{'name': 'REGISTRY_STORAGE_FILESYSTEM_ROOTDIRECTORY', 'value': root},
                                {'name': 'REGISTRY_STORAGE_DELETE_ENABLED', 'value': 'true'}],
                        'securityContext': registry.get('securityContext', {}),
                        'resources': registry.get('resources', {}),
                        'volumeMounts': [{'name': 'data', 'mountPath': root}]}],
                    'volumes': [data]}}}}

    async def collect_garbage(self, now=None):
        """1日1回、決めた時刻に内部Registryの使わなくなったデータを消す。"""
        hour = self.settings.registry_gc_hour
        now = now or datetime.now(timezone.utc)
        if hour < 0 or now.hour != hour:
            return None
        day = now.date().isoformat()
        if ((await self.record('registry-gc')) or {}).get('day') == day:
            return None
        namespace = self.settings.registry_namespace
        try:
            deployment = await self.kube('GET', 'deployments', 'registry', group='apis/apps/v1', namespace=namespace)
        except HTTPException:
            deployment = None  # 権限が無い＝内部Registryを配備していない
        if not deployment:
            return None  # 内部Registryを使わない構成（Artifact Registryなど）
        async with self.lock:
            records = await self.kube('GET', 'configmaps', query='?labelSelector=koyorina-record%3Dpublication')
            if any(r['metadata']['name'].startswith('build-') and json.loads(r['data']['state'])['status'] in ACTIVE_BUILDS
                   for r in (records or {}).get('items', [])):
                return None  # ビルド中。この時間のうちに、空いたところでやり直す
            self.maintenance = True
        name = 'registry-gc-' + now.strftime('%Y%m%d')
        status = 'failed'
        try:
            await self.kube('DELETE', 'jobs', name, group='apis/batch/v1', namespace=namespace,
                            body={'propagationPolicy': 'Foreground'})
            await self.kube('POST', 'jobs', group='apis/batch/v1', namespace=namespace,
                            body=self.gc_job(name, deployment))
            deadline = stamp() + self.settings.registry_gc_timeout + 60
            while stamp() < deadline:
                job = await self.kube('GET', 'jobs', name, group='apis/batch/v1', namespace=namespace)
                result = (job or {}).get('status', {})
                if result.get('succeeded') or result.get('failed'):
                    status = 'succeeded' if result.get('succeeded') else 'failed'
                    break
                await asyncio.sleep(10)
        finally:
            self.maintenance = False
            await self.record('registry-gc', {'day': day, 'status': status, 'finished': stamp()})
        return status

    async def publish(self, project_id, payload):
        async with self.lock:
            name = 'published-' + str(project_id)
            build = await self.record('build-' + str(payload.build_id))
            if (not build or build['status'] != 'succeeded' or build['project_id'] != str(project_id)
                    or build['tenant_id'] != str(payload.tenant_id)
                    or payload.image != build['image'].rsplit(':', 1)[0] + '@' + build['digest']):
                raise HTTPException(409, 'Only completed project builds may be published')
            state = await self.record(name)
            if state and state['status'] in ('starting', 'updating', 'stopping', 'deleting'):
                raise HTTPException(409, 'Publication in progress')
            await self.ensure_transport(build.get('registry'))
            resources = payload.resources or self.default_resources()
            await self.claim(name + '-data', f'{resources.storage_gi}Gi', runtime=True)
            secret_name = name + '-env-' + str(uuid4())[:8]
            values = {**payload.environment, 'APP_ORIGIN': payload.app_origin,
                'APP_BASE_PATH': published_base(project_id), 'APP_FORWARD_SECRET': payload.forward_secret,
                'APP_SESSION_SECRET': hashlib.sha256((payload.forward_secret + ':session').encode()).hexdigest(),
                'DATABASE_URL': 'sqlite+pysqlite:////var/published/db/app.db', 'PREVIEW_VAR': '/var/published'}
            await self.kube('PATCH', 'secrets', secret_name, runtime=True, body={
                'apiVersion': 'v1', 'kind': 'Secret', 'metadata': {'name': secret_name},
                'data': {k: base64.b64encode(v.encode()).decode() for k, v in values.items()}})
            manifest = self.runtime_manifest(name, payload.image, secret_name, resources)
            registry = build.get('registry')
            if registry and (registry['kind'] == 'artifact' or registry.get('auth_secret')):
                pull = await self.ensure_pull(registry)
                manifest['spec']['template']['spec']['imagePullSecrets'] = [{'name': pull}]
            # A stopped app has no live version to roll back to. Reusing its old
            # manifest as previous would retry the same broken image indefinitely.
            previous = state.get('current') if state and state.get('status') == 'running' else None
            new = {'build_id': str(payload.build_id), 'manifest': manifest, 'registry': registry}
            state = {'project_id': str(project_id), 'tenant_id': str(payload.tenant_id),
                'status': 'updating' if previous else 'starting', 'build_id': previous['build_id'] if previous else None,
                'previous': previous, 'pending': new, 'started': stamp(), 'error': None}
            # Persist intent first; reconcile applies it after restart if needed.
            await self.record(name, state)
            await self.apply_runtime(name, manifest)
            return state

    def runtime_manifest(self, name, image, secret, resources=None):
        s = self.settings
        resources = resources or self.default_resources()
        pod = {'automountServiceAccountToken': False, 'enableServiceLinks': False,
            'securityContext': {'runAsNonRoot': True, 'runAsUser': 10001, 'runAsGroup': 10001,
                'fsGroup': 10001, 'seccompProfile': {'type': 'RuntimeDefault'}},
            'containers': [{'name': 'app', 'image': image,
                'envFrom': [{'secretRef': {'name': secret}}],
                'securityContext': {'readOnlyRootFilesystem': True, 'allowPrivilegeEscalation': False,
                    'capabilities': {'drop': ['ALL']}},
                'ports': [{'containerPort': 8080}],
                'readinessProbe': {'httpGet': {'path': '/__preview/health', 'port': 8080}, 'periodSeconds': 3},
                'livenessProbe': {'httpGet': {'path': '/__preview/health', 'port': 8080},
                    'initialDelaySeconds': 60, 'periodSeconds': 10},
                'resources': resources.container_resources(),
                'volumeMounts': [{'name': 'data', 'mountPath': '/var/published'},
                    {'name': 'tmp', 'mountPath': '/tmp'}]}],
            'volumes': [{'name': 'data', 'persistentVolumeClaim': {'claimName': name + '-data'}},
                {'name': 'tmp', 'emptyDir': {'sizeLimit': '256Mi'}}]}
        if s.pull_secret:
            pod['imagePullSecrets'] = [{'name': s.pull_secret}]
        pod['tolerations'] = [{'key': 'workload', 'operator': 'Equal', 'value': s.app_name + '-agent', 'effect': 'NoSchedule'}]
        if s.node:
            pod['nodeSelector'] = {'kubernetes.io/hostname': s.node}
        return {'apiVersion': 'apps/v1', 'kind': 'Deployment',
            'metadata': {'name': name, 'namespace': s.runtime_namespace},
            'spec': {'replicas': 1, 'strategy': {'type': 'Recreate'},
                'selector': {'matchLabels': {'koyorina-published': name}},
                'template': {'metadata': {'labels': {'koyorina-published': name}}, 'spec': pod}}}

    async def apply_runtime(self, name, manifest):
        await self.kube('PATCH', 'services', name, runtime=True, body={'apiVersion': 'v1', 'kind': 'Service',
            'metadata': {'name': name}, 'spec': {'selector': {'koyorina-published': name},
                'ports': [{'port': 8080, 'targetPort': 8080}]}})
        await self.kube('PATCH', 'deployments', name, body=manifest, group='apis/apps/v1', runtime=True)

    async def tick_publication(self, name, state):
        if state['status'] == 'stopped':
            await self.kube('DELETE', 'deployments', name, runtime=True, group='apis/apps/v1')
            return
        for version in ('current', 'pending', 'previous'):
            registry = (state.get(version) or {}).get('registry')
            if registry and (registry['kind'] == 'artifact' or registry.get('auth_secret')):
                await self.ensure_pull(registry)
        desired = state.get('pending') if state['status'] in ('starting', 'updating') else state.get('current')
        if not desired:
            return
        deployment = await self.kube('GET', 'deployments', name, runtime=True, group='apis/apps/v1')
        spec = desired['manifest']['spec']
        if not deployment or deployment['spec']['template'] != spec['template']:
            # Server adds defaults; compare only the desired image and secret when deciding to apply.
            actual = (deployment or {}).get('spec', {}).get('template', {}).get('spec', {}).get('containers', [{}])[0]
            expected = spec['template']['spec']['containers'][0]
            if not deployment or actual.get('image') != expected['image'] or actual.get('envFrom') != expected['envFrom']:
                await self.apply_runtime(name, desired['manifest'])
                return
        status = deployment.get('status', {})
        ready = (status.get('observedGeneration', 0) >= deployment['metadata'].get('generation', 0)
            and status.get('updatedReplicas') == 1 and status.get('readyReplicas') == 1
            and status.get('availableReplicas') == 1)
        if ready:
            state.update(status='running', current=desired, pending=None, build_id=desired['build_id'])
            await self.record(name, state)
        elif state['status'] in ('starting', 'updating') and stamp() - state['started'] > self.settings.startup_timeout:
            pods = await self.kube('GET', 'pods', runtime=True,
                query='?labelSelector=koyorina-published%3D' + name)
            problems = [pod_problem(pod) for pod in (pods or {}).get('items', [])]
            reason, message = next((problem for problem in problems if problem[0]), ('StartupTimeout', '起動確認が時間内に完了しませんでした。'))
            detail = f'{reason}: {message}'[:1200]
            previous = state.get('previous')
            if previous:
                await self.ensure_transport(previous.get('registry'))
                await self.apply_runtime(name, previous['manifest'])
                state.update(status='starting', pending=previous, previous=None, started=stamp(),
                    error='公開版の起動に失敗したため、直前の版へ戻しています。 ' + detail)
            else:
                await self.kube('DELETE', 'deployments', name, runtime=True, group='apis/apps/v1')
                state.update(status='failed', pending=None, error='アプリの起動に失敗しました。データは保持しています。 ' + detail)
            await self.record(name, state)
        elif state['status'] == 'running' and not ready:
            # Keep reconciling an unhealthy published deployment, but don't advertise it as ready.
            state.update(status='starting', pending=state['current'], previous=None, started=stamp())
            await self.record(name, state)

    async def stop(self, project_id):
        async with self.lock:
            name = 'published-' + str(project_id)
            state = await self.record(name)
            if not state:
                return {'status': 'stopped'}
            state.update(status='stopped', pending=None)
            await self.record(name, state)
            await self.kube('DELETE', 'deployments', name, runtime=True, group='apis/apps/v1')
            # Service, secrets and PVC are kept, for restart and rollback.
            return state

    async def purge(self, project_id):
        """Remove only a stopped publication and its data; builds remain available."""
        async with self.lock:
            return await self._purge_locked(project_id)

    async def _purge_locked(self, project_id):
        name = 'published-' + str(project_id)
        state = await self.record(name)
        if state and state['status'] not in ('stopped', 'deleting'):
            raise HTTPException(409, 'Stop the published app before deleting its data')
        deployment = await self.kube('GET', 'deployments', name, runtime=True, group='apis/apps/v1')
        pods = await self.kube('GET', 'pods', runtime=True,
            query='?labelSelector=koyorina-published%3D' + name)
        if deployment or (pods or {}).get('items'):
            raise HTTPException(409, 'Published app pods must be gone before deleting data')
        if state and state['status'] != 'deleting':
            state['status'] = 'deleting'
            await self.record(name, state)
        await self.kube('DELETE', 'services', name, runtime=True)
        secrets = await self.kube('GET', 'secrets', runtime=True)
        for secret in (secrets or {}).get('items', []):
            secret_name = secret.get('metadata', {}).get('name', '')
            if secret_name.startswith(name + '-env-'):
                await self.kube('DELETE', 'secrets', secret_name, runtime=True)
        await self.kube('DELETE', 'persistentvolumeclaims', name + '-data', runtime=True)
        if await self.kube('GET', 'persistentvolumeclaims', name + '-data', runtime=True):
            raise HTTPException(503, 'Publication data volume deletion is still pending')
        await self.kube('DELETE', 'configmaps', name)
        return {'status': 'deleted'}

    async def move_tenant(self, project_id, payload):
        """Rebind stopped publication records; the project PVC stays in place."""
        async with self.lock:
            project = str(project_id)
            source, target = str(payload.source_tenant_id), str(payload.target_tenant_id)
            publication = await self.record('published-' + project)
            if publication:
                if publication.get('project_id') != project or publication.get('status') != 'stopped':
                    raise HTTPException(409, 'Publication must be stopped before tenant migration')
                if publication.get('tenant_id') not in (source, target):
                    raise HTTPException(409, 'Publication tenant does not match migration')
            if await self.kube('GET', 'deployments', 'published-' + project,
                    runtime=True, group='apis/apps/v1') is not None:
                raise HTTPException(409, 'Published deployment is still present')
            builds = []
            for build_id in payload.build_ids:
                state = await self.record('build-' + str(build_id))
                if not state or state.get('project_id') != project or state.get('tenant_id') not in (source, target):
                    raise HTTPException(409, 'Build record does not match migration')
                if state.get('status') in ACTIVE_BUILDS:
                    raise HTTPException(409, 'Build is still active')
                builds.append((build_id, state))
            for build_id, state in builds:
                if state['tenant_id'] != target:
                    state['tenant_id'] = target
                    await self.record('build-' + str(build_id), state)
            if publication and publication['tenant_id'] != target:
                publication['tenant_id'] = target
                await self.record('published-' + project, publication)
            return {'status': 'moved'}

    async def reconcile(self):
        async with self.lock:
            records = await self.kube('GET', 'configmaps', query='?labelSelector=koyorina-record%3Dpublication')
            states = [(r['metadata']['name'], json.loads(r['data']['state'])) for r in (records or {}).get('items', [])]
            builds = sorted([(n, s) for n, s in states if n.startswith('build-') and s['status'] in ACTIVE_BUILDS],
                            key=lambda item: item[1]['created'])
            running = [s for _, s in builds if s['status'] != 'queued']
            for state in running:
                await self.tick_build(state)
            active = [s for s in running if s['status'] in ACTIVE_BUILDS]
            for name, state in builds:
                if not state.get('snapshot_ready', True):
                    if stamp() - state['created'] > 300:
                        state.update(status='failed', error='ソースの転送が完了しませんでした。再実行してください。')
                        await self.record(name, state)
                        await self.cleanup_build(state)
                    continue
                if state['status'] != 'queued' or len(active) >= self.settings.max_builds:
                    continue
                if any(s['tenant_id'] == state['tenant_id'] for s in active):
                    continue  # same RWO cache claim: one writer per tenant
                try:
                    await self.tick_build(state)
                    active.append(state)
                except Exception:
                    state.update(status='failed', error='ビルド環境を準備できません。管理者に設定を確認してください。')
                    await self.record(name, state)
                    await self.cleanup_build(state)
            for name, state in states:
                if name.startswith('build-') and state['status'] not in ACTIVE_BUILDS and not state.get('cleaned'):
                    await self.cleanup_build(state)
            for name, state in states:
                if name.startswith('published-'):
                    try:
                        if state['status'] == 'deleting':
                            await self._purge_locked(UUID(state['project_id']))
                        else:
                            await self.tick_publication(name, state)
                    except Exception:
                        import logging
                        logging.getLogger('koyorina.publication').warning('Publication authentication/reconciliation failed; retrying')


def create_controller(settings=None):
    settings = settings or ControllerSettings()
    controller = Controller(settings)

    @asynccontextmanager
    async def lifespan(app):
        async def loop():
            while True:
                try:
                    await controller.reconcile()
                except Exception:
                    # Reconciliation retries persisted intents; never turn an outage into success.
                    import logging
                    logging.getLogger('koyorina.publication').warning('Reconciliation failed; retrying')
                await asyncio.sleep(3)
        async def maintenance():
            # 状態合わせと分ける。GCは数分かかり、その間も公開アプリの状態は追い続ける。
            while True:
                try:
                    await controller.collect_garbage()
                except Exception:
                    import logging
                    logging.getLogger('koyorina.publication').warning('Registry garbage collection failed; retrying tomorrow')
                await asyncio.sleep(60)
        tasks = [asyncio.create_task(loop()), asyncio.create_task(maintenance())]
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.controller = controller

    @app.middleware('http')
    async def authenticate(request: Request, next):
        if request.url.path != '/healthz' and not hmac.compare_digest(
                request.headers.get('authorization', ''), 'Bearer ' + settings.token.get_secret_value()):
            from fastapi.responses import JSONResponse
            return JSONResponse({'detail': 'Unauthorized'}, status_code=401)
        return await next(request)

    @app.get('/healthz')
    async def health():
        return {'ok': True}

    @app.get('/registry/workload-identity')
    async def identity():
        def read():
            discovery = k8s_token.cluster_get('/.well-known/openid-configuration')
            return {'issuer': discovery['issuer'], 'jwks': k8s_token.cluster_get('/openid/v1/jwks'),
                    'writer_subject': f'system:serviceaccount:{settings.build_namespace}:publication-controller',
                    'reader_subject': f'system:serviceaccount:{settings.build_namespace}:publication-puller'}
        return await run_in_threadpool(read)

    @app.post('/registry/test')
    async def registry_test(payload: RegistrySelection):
        return await controller.registry_test(payload)

    @app.post('/builds/{build_id}', status_code=202)
    async def submit(build_id: UUID, payload: BuildInput):
        return await controller.submit(build_id, payload)

    @app.get('/builds/{build_id}')
    async def status(build_id: UUID):
        state = await controller.record('build-' + str(build_id))
        if not state:
            raise HTTPException(404)
        return {k: v for k, v in state.items() if k != 'logs'}

    @app.get('/dockerfile')
    async def dockerfile():
        return await controller.build_dockerfile()

    @app.get('/builds/{build_id}/dockerfile')
    async def build_dockerfile(build_id: UUID):
        state = await controller.record('build-' + str(build_id))
        if not state:
            raise HTTPException(404)
        return await controller.build_dockerfile(state)

    @app.get('/builds/{build_id}/logs')
    async def logs(build_id: UUID):
        state = await controller.record('build-' + str(build_id))
        if not state:
            raise HTTPException(404)
        return {'logs': await controller.build_logs(state)}

    @app.delete('/builds/{build_id}')
    async def cancel(build_id: UUID):
        return await controller.cancel(build_id)

    @app.post('/projects/{project_id}', status_code=202)
    async def publish(project_id: UUID, payload: PublishInput):
        state = await controller.publish(project_id, payload)
        return {k: state.get(k) for k in ('status', 'build_id', 'error')}

    @app.get('/projects/{project_id}/resources')
    async def resources(project_id: UUID):
        return await controller.resource_info(project_id)

    @app.get('/projects/{project_id}')
    async def published(project_id: UUID):
        state = await controller.record('published-' + str(project_id))
        if not state:
            raise HTTPException(404)
        return {k: state.get(k) for k in ('status', 'build_id', 'error')}

    @app.delete('/projects/{project_id}')
    async def stop(project_id: UUID):
        state = await controller.stop(project_id)
        return {k: state.get(k) for k in ('status', 'build_id', 'error')}

    @app.delete('/projects/{project_id}/data')
    async def purge(project_id: UUID):
        return await controller.purge(project_id)

    @app.post('/projects/{project_id}/images/prune')
    async def prune(project_id: UUID, payload: PruneInput):
        return await controller.prune_images(project_id, payload)

    @app.post('/projects/{project_id}/tenant-move')
    async def move_tenant(project_id: UUID, payload: TenantMoveInput):
        return await controller.move_tenant(project_id, payload)
    return app
