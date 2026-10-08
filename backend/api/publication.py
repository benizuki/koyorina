"""Build, publish, grants and environment. Long operations never hold a DB session."""
import base64
import json
from uuid import UUID, uuid4
from datetime import datetime, timezone
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import delete, or_, select
from starlette.concurrency import run_in_threadpool
from backend.api.projects import editable, operable, readable, tenant_member
from backend.api.generation import job_bundle
from backend.core.app_session import origin_of, url_of
from backend.core.auth import actor
from backend.core.db import (AppBuild, AppPublication, Audit, Department, GenerationJob,
    Project, PublicationEvent, PublicationGrant, Tenant, TenantMigration, User, UserTenant, SystemSetting)
from backend.core.publication_client import call
from backend.core.cluster import clean_log_line
from backend.core import cluster as cluster_reader
from backend.core.secret_box import open_, seal, SecretBoxUnavailable
from backend.domain import preview_env
from backend.domain.publication import (published_secret, ACTIVE_BUILDS, image_reference, permitted, retention,
                                        snapshot, PublicationResources)
from backend.domain.preview import forward_secret
from backend.domain.roles import can_manage, can_operate_tenant, operator_tenant_ids, tenant_role_set

from backend.domain import system_registry


def registry_config(request):
    with request.app.state.sessions() as db:
        row = db.get(SystemSetting, system_registry.KEY)
        try:
            return system_registry.selection(row.value if row else None, request.app.state.settings.tenant_secret_key.get_secret_value())
        except SecretBoxUnavailable as exc:
            raise HTTPException(503, str(exc)) from None


router = APIRouter()


def resolve(request, project_id, mode='read'):
    with request.app.state.sessions() as db:
        user = actor(request, db)
        checker = {'read': readable, 'write': editable, 'stop': operable}[mode]
        project = checker(db, project_id, user)
        if mode == 'write' and db.scalar(select(TenantMigration.id).where(
                TenantMigration.project_id == project.id, TenantMigration.status == 'copying')):
            raise HTTPException(409, 'テナント移行中はビルド・公開設定を変更できません。')
        return project, user


def mutable(db, request, project_id):
    project = editable(db, project_id, actor(request, db))
    if db.scalar(select(TenantMigration.id).where(TenantMigration.project_id == project.id,
            TenantMigration.status == 'copying')):
        raise HTTPException(409, 'テナント移行中は公開操作を変更できません。')
    return project


def operational(db, request, project_id, *, mutation=False):
    user = actor(request, db)
    project = db.get(Project, str(project_id))
    if not project or not can_operate_tenant(db, user, project.tenant_id):
        raise HTTPException(404, '公開アプリが見つかりません。')
    if mutation and db.scalar(select(TenantMigration.id).where(TenantMigration.project_id == project.id,
            TenantMigration.status == 'copying')):
        raise HTTPException(409, 'テナント移行中は公開操作を変更できません。')
    return project, user


def stored_error(value: str | None) -> str | None:
    """既存DBの300文字制限内に、理由の先頭と原因の末尾を保存する。"""
    if value is None:
        return None
    value = clean_log_line(value)
    return value if len(value) <= 300 else value[:100] + ' … ' + value[-197:]


def build_view(row):
    result = {key: getattr(row, key) for key in ('id', 'generation_id', 'revision', 'actor_id',
        'source_hash', 'registry_kind', 'image', 'digest', 'status', 'error', 'created_at', 'updated_at')}
    created = row.created_at.replace(tzinfo=timezone.utc) if row.created_at.tzinfo is None else row.created_at
    end = datetime.now(timezone.utc) if row.status in ACTIVE_BUILDS else row.updated_at
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    result['duration_seconds'] = max(0, int((end - created).total_seconds()))
    return result


async def prune(request, project_id, *, everything=False):
    """保持ルールから外れたビルドのイメージと履歴を消す。消せなかったビルドの数を返す。

    everything はプロジェクトの削除用（公開アプリは削除済みであること）。同じプロジェクトで
    ビルドが動いている間は消さない。同じ中身のビルドは同じダイジェストになるので、
    いま push 中のイメージを消してしまうことがある。

    消すと決めたビルドは、プロジェクトの行をロックしたまま 'pruning' にしてから消す。
    公開の確定（release）も同じ行をロックするので、消している最中のビルドを公開に選べない。
    プロジェクトの削除は自身がその行をロックしているので、ここでは取らない（待ち続けてしまう）。
    """
    settings = request.app.state.settings
    def plan():
        with request.app.state.sessions() as db:
            if not everything:
                db.get(Project, str(project_id), with_for_update=True)
            rows = list(db.scalars(select(AppBuild).where(AppBuild.project_id == str(project_id))))
            if any(row.status in ACTIVE_BUILDS for row in rows):
                return {}, []
            if everything:
                remove, protected = rows, set()
            else:
                publication = db.get(AppPublication, str(project_id))
                remove, protected = retention(rows, publication.build_id if publication else None,
                                              settings.publication_keep_builds)
            reserved = {row.id: (row.status, row.digest) for row in remove}
            for row in remove:
                row.status = 'pruning'
            db.commit()
            return reserved, sorted(protected)
    reserved, protected = await run_in_threadpool(plan)
    if not reserved:
        return 0
    done = set()
    try:
        result = await call(settings, 'POST', f'/projects/{project_id}/images/prune', {
            'builds': [{'id': build_id, 'digest': digest} for build_id, (_, digest) in reserved.items()],
            'keep_digests': protected})
        done = set((result or {}).get('removed', [])) & set(reserved)
    finally:
        def settle():
            with request.app.state.sessions() as db:
                if done:
                    db.execute(delete(AppBuild).where(AppBuild.project_id == str(project_id), AppBuild.id.in_(done)))
                    db.add(Audit(action='build.pruned', resource_id=str(project_id), detail=f'{len(done)} builds'))
                # 消せなかったものは元に戻し、次の機会にやり直す。
                for build_id, (status, _) in reserved.items():
                    row = db.get(AppBuild, build_id)
                    if build_id not in done and row is not None and row.status == 'pruning':
                        row.status = status
                db.commit()
        await run_in_threadpool(settle)
    return len(reserved) - len(done)


async def sync(request, project):
    settings = request.app.state.settings
    if not settings.publication_enabled:
        return
    def pending():
        with request.app.state.sessions() as db:
            return [(row.id, row.created_at) for row in db.scalars(select(AppBuild).where(
                AppBuild.project_id == project.id, AppBuild.status.in_(ACTIVE_BUILDS)))]
    succeeded = False
    for build_id, created in await run_in_threadpool(pending):
        state = await call(settings, 'GET', f'/builds/{build_id}')
        if state is None:
            age = datetime.now(timezone.utc) - (created.replace(tzinfo=timezone.utc) if created.tzinfo is None else created)
            if age.total_seconds() < 300:
                continue
            state = {'status': 'failed', 'error': '実行基盤にビルド記録がありません。再実行してください。'}
        if state is not None:
            def save():
                with request.app.state.sessions() as db:
                    row = db.get(AppBuild, build_id, with_for_update=True)
                    if row and row.status in ACTIVE_BUILDS:
                        if row.status != state['status'] and state['status'] not in ACTIVE_BUILDS:
                            db.add(Audit(actor_id=row.actor_id, action='build.' + state['status'], resource_id=row.id))
                        row.status, row.digest, row.error = state['status'], state.get('digest'), stored_error(state.get('error'))
                        db.commit()
                        return row.status == 'succeeded'
            succeeded = await run_in_threadpool(save) or succeeded
    state = await call(settings, 'GET', f'/projects/{project.id}')
    if state:
        def save_publication():
            with request.app.state.sessions() as db:
                row = db.get(AppPublication, project.id, with_for_update=True)
                if row:
                    if row.status != state['status'] and state['status'] in ('running', 'failed'):
                        event = db.scalar(select(PublicationEvent).where(PublicationEvent.project_id == project.id).order_by(PublicationEvent.created_at.desc()))
                        if event:
                            db.add(Audit(actor_id=event.actor_id, action='publication.' + state['status'], resource_id=project.id, detail=state.get('build_id')))
                    row.status, row.error = state['status'], stored_error(state.get('error'))
                    if state['status'] in ('starting', 'updating', 'running') and state.get('build_id'):
                        row.build_id = state['build_id']
                    db.commit()
        await run_in_threadpool(save_publication)
    if succeeded:
        # 新しい版ができたときに古い版を片付ける。失敗しても次の成功でやり直す。
        try:
            await prune(request, project.id)
        except Exception:
            import logging
            logging.getLogger('koyorina.publication').warning('Image pruning failed; retrying after the next build')


@router.get('/api/projects/{project_id}/publication')
async def status(project_id: UUID, request: Request):
    project, user = await run_in_threadpool(resolve, request, project_id)
    await sync(request, project)
    registry = await run_in_threadpool(registry_config, request)
    def read():
        with request.app.state.sessions() as db:
            row = db.get(AppPublication, project.id)
            return {'enabled': request.app.state.settings.publication_enabled,
                'registry_kind': registry.kind if registry else request.app.state.settings.app_registry_kind,
                'registry_host': registry.host if registry and registry.host else request.app.state.settings.app_registry_host,
                'scanning_enabled': registry.scanning_enabled if registry else request.app.state.settings.app_registry_scanning_enabled,
                'status': row.status if row else 'stopped', 'build_id': row.build_id if row else None,
                'error': row.error if row else None, 'url': url_of(request.app.state.settings, project.id, 'published'),
                'builds': [build_view(b) for b in db.scalars(select(AppBuild).where(
                    AppBuild.project_id == project.id).order_by(AppBuild.created_at.desc()))],
                'history': [{'action': e.action, 'build_id': e.build_id, 'at': e.created_at}
                    for e in db.scalars(select(PublicationEvent).where(
                        PublicationEvent.project_id == project.id).order_by(PublicationEvent.created_at.desc()).limit(50))]}
    return await run_in_threadpool(read)


class BuildInput(BaseModel):
    generation_id: UUID


@router.post('/api/projects/{project_id}/builds', status_code=202)
async def build(project_id: UUID, payload: BuildInput, request: Request):
    project, user = await run_in_threadpool(resolve, request, project_id, 'write')
    settings = request.app.state.settings
    if not settings.publication_enabled:
        raise HTTPException(503, 'アプリの公開機能は無効です。')
    def generation():
        with request.app.state.sessions() as db:
            row = db.get(GenerationJob, str(payload.generation_id))
            if not row or row.project_id != project.id:
                raise HTTPException(404, '生成版が見つかりません。')
            return row
    job = await run_in_threadpool(generation)
    bundle = await job_bundle(settings, user, job, project.tenant_id)
    archive, source_hash = await run_in_threadpool(snapshot, bundle)
    build_id = str(uuid4())
    registry = await run_in_threadpool(registry_config, request)
    if registry is None and settings.app_registry_kind == 'unconfigured':
        raise HTTPException(409, 'システム設定でイメージの保存先を設定してください。')
    if registry is None and settings.app_registry_kind == 'artifact':
        raise HTTPException(409, 'システム設定でArtifact RegistryのWIF設定を保存してください。')
    kind = registry.kind if registry else settings.app_registry_kind
    host = registry.host if registry and registry.host else settings.app_registry_host
    if (settings.app_env == 'local' and settings.publication_controller_url == 'http://publication-controller:8080'
            and (kind != 'private' or host != settings.app_registry_host)):
        raise HTTPException(409, 'Compose版はローカルRegistryでビルドします。')
    if registry and registry.kind == 'private' and settings.app_registry_kind != 'private':
        raise HTTPException(409, '内部RegistryをAnsibleで構成してください。')
    image = image_reference(host, project.id, build_id)
    def reserve():
        with request.app.state.sessions() as db:
            mutable(db, request, project.id)  # lock project across reservation
            if db.scalar(select(AppBuild.id).where(AppBuild.project_id == project.id,
                    AppBuild.status.in_(ACTIVE_BUILDS))):
                raise HTTPException(409, 'このアプリのビルドを実行中です。')
            row = AppBuild(id=build_id, project_id=project.id, tenant_id=project.tenant_id,
                generation_id=job.id, revision=job.revision, actor_id=user.id,
                source_hash=source_hash, registry_kind=kind, image=image)
            db.add(row)
            db.add(Audit(actor_id=user.id, action='build.requested', resource_id=build_id))
            db.commit()
    await run_in_threadpool(reserve)
    try:
        await call(settings, 'POST', f'/builds/{build_id}', {'tenant_id': project.tenant_id,
            'project_id': project.id, 'revision': job.revision,
            'source': base64.b64encode(archive).decode(), 'source_hash': source_hash,
            **({'registry': registry.model_dump()} if registry else {})})
    except HTTPException:
        # Request may have reached the controller; query before marking dispatch as failed.
        try:
            found = await call(settings, 'GET', f'/builds/{build_id}')
        except HTTPException:
            found = {'status': 'queued'}
        if found is None:
            def fail():
                with request.app.state.sessions() as db:
                    row = db.get(AppBuild, build_id, with_for_update=True)
                    row.status, row.error = 'failed', 'ビルド要求を実行基盤へ届けられませんでした。'
                    db.commit()
            await run_in_threadpool(fail)
        raise
    return {'id': build_id, 'status': 'queued'}


async def owned_build(request, project_id, build_id, mode='read'):
    project, user = await run_in_threadpool(resolve, request, project_id, mode)
    def read():
        with request.app.state.sessions() as db:
            row = db.get(AppBuild, str(build_id))
            if not row or row.project_id != project.id:
                raise HTTPException(404, 'ビルドが見つかりません。')
            return row
    return project, user, await run_in_threadpool(read)


@router.get('/api/projects/{project_id}/publication/dockerfile')
async def current_dockerfile(project_id: UUID, request: Request):
    await run_in_threadpool(resolve, request, project_id)
    return await call(request.app.state.settings, 'GET', '/dockerfile') or {'dockerfile': '', 'recorded': False}


@router.get('/api/projects/{project_id}/builds/{build_id}/dockerfile')
async def build_dockerfile(project_id: UUID, build_id: UUID, request: Request):
    await owned_build(request, project_id, build_id)
    return await call(request.app.state.settings, 'GET', f'/builds/{build_id}/dockerfile') or {'dockerfile': '', 'recorded': False}


@router.get('/api/projects/{project_id}/builds/{build_id}/logs')
async def logs(project_id: UUID, build_id: UUID, request: Request):
    await owned_build(request, project_id, build_id)
    return await call(request.app.state.settings, 'GET', f'/builds/{build_id}/logs') or {'logs': ''}


@router.post('/api/projects/{project_id}/builds/{build_id}/cancel')
async def cancel(project_id: UUID, build_id: UUID, request: Request):
    project, user, row = await owned_build(request, project_id, build_id, 'write')
    result = await call(request.app.state.settings, 'DELETE', f'/builds/{build_id}')
    def save():
        with request.app.state.sessions() as db:
            b = db.get(AppBuild, str(build_id))
            if b.status in ACTIVE_BUILDS:
                b.status = (result or {}).get('status', 'cancelled')
                db.add(Audit(actor_id=user.id, action='build.cancelled', resource_id=b.id))
                db.commit()
    await run_in_threadpool(save)
    return result or {'status': 'cancelled'}


class PublishInput(BaseModel):
    build_id: UUID


RELEASE_ACTIONS = ('release', 'publish', 'start')


def registered_build(db, project_id, publication):
    """Find the registered version, including older publications with no saved build ID."""
    if publication is None:
        return None
    last = db.scalar(select(PublicationEvent).where(
        PublicationEvent.project_id == project_id,
        PublicationEvent.action.in_(RELEASE_ACTIONS),
        PublicationEvent.build_id.is_not(None)).order_by(
            PublicationEvent.created_at.desc(), PublicationEvent.id.desc()).limit(1))
    for build_id in (publication.build_id, last.build_id if last else None):
        build = db.get(AppBuild, build_id) if build_id else None
        if build and build.project_id == project_id and build.status == 'succeeded' and build.digest:
            return build
    return None


@router.post('/api/projects/{project_id}/publication/release', status_code=202)
def release(project_id: UUID, payload: PublishInput, request: Request):
    """Register a built version for operations without starting a runtime."""
    if not request.app.state.settings.publication_enabled:
        raise HTTPException(503, 'アプリの公開機能は無効です。')
    with request.app.state.sessions() as db:
        project = mutable(db, request, project_id)
        user = actor(request, db)
        build = db.get(AppBuild, str(payload.build_id))
        if not build or build.project_id != project.id:
            raise HTTPException(404, 'ビルドが見つかりません。')
        if build.status != 'succeeded' or not build.digest:
            raise HTTPException(409, 'Pushが完了したビルドを選んでください。')
        pub = db.get(AppPublication, project.id, with_for_update=True)
        if pub and pub.status != 'stopped':
            raise HTTPException(409, '稼働中の公開版を変更する場合は、公開アプリ運用で停止してください。')
        if pub is None:
            pub = AppPublication(project_id=project.id)
            db.add(pub)
        pub.build_id, pub.status, pub.error = build.id, 'stopped', None
        db.add(PublicationEvent(project_id=project.id, build_id=build.id, actor_id=user.id, action='release'))
        db.add(Audit(actor_id=user.id, action='publication.released', resource_id=project.id, detail=build.id))
        db.commit()
    return {'status': 'stopped', 'build_id': str(payload.build_id)}


def environment(request, project_id):
    with request.app.state.sessions() as db:
        row = db.get(AppPublication, str(project_id))
        saved = row.environment if row else []
    key = request.app.state.settings.tenant_secret_key.get_secret_value()
    return {e['name']: open_(key, e['value']) if e['secret'] else e['value'] for e in saved}


@router.post('/api/projects/{project_id}/publication', status_code=202)
async def publish(project_id: UUID, payload: PublishInput, request: Request):
    def selected():
        with request.app.state.sessions() as db:
            project, user = operational(db, request, project_id, mutation=True)
            pub = db.get(AppPublication, project.id)
            if not registered_build(db, project.id, pub):
                raise HTTPException(409, '先に公開タブでビルド済みの版を公開してください。')
            row = db.get(AppBuild, str(payload.build_id))
            if not row or row.project_id != project.id:
                raise HTTPException(404, 'ビルドが見つかりません。')
            if row.id != pub.build_id and not db.scalar(select(PublicationEvent.id).where(
                    PublicationEvent.project_id == project.id, PublicationEvent.build_id == row.id,
                    PublicationEvent.action.in_(('release', 'publish')))):
                raise HTTPException(409, '先に公開タブでこの版を公開してください。')
            return project, user, row
    project, user, row = await run_in_threadpool(selected)
    return await publish_build(request, project, user, row, action='publish')


async def publish_build(request, project, user, row, *, action):
    await sync(request, project)
    def reserve():
        with request.app.state.sessions() as db:
            operational(db, request, project.id, mutation=True)
            b = db.get(AppBuild, row.id)
            if b.status != 'succeeded' or not b.digest:
                raise HTTPException(409, 'Pushが完了したビルドを選んでください。')
            pub = db.get(AppPublication, project.id, with_for_update=True)
            if pub and pub.status in ('starting', 'updating', 'stopping', 'deleting'):
                raise HTTPException(409, '公開操作を実行中です。')
            if not pub:
                pub = AppPublication(project_id=project.id)
                db.add(pub)
            previous = pub.status
            pub.status, pub.error = 'starting', None
            db.commit()
            return b.image.rsplit(':', 1)[0] + '@' + b.digest, previous, pub.resources or None
    image, previous, resources = await run_in_threadpool(reserve)
    try:
        extra = await run_in_threadpool(environment, request, project.id)
        result = await call(request.app.state.settings, 'POST', f'/projects/{project.id}',
            {'tenant_id': project.tenant_id, 'build_id': row.id, 'image': image,
             'app_origin': origin_of(request.app.state.settings, project.id, 'published'),
             'forward_secret': published_secret(project.id, request.app.state.settings.app_session_secret),
             'environment': extra, 'resources': resources})
    except (HTTPException, SecretBoxUnavailable) as exc:
        guidance = isinstance(exc, HTTPException) and exc.status_code == 422
        failure = exc.detail if guidance else '公開要求を確認できません。状態を更新してください。'
        def restore():
            with request.app.state.sessions() as db:
                pub = db.get(AppPublication, project.id)
                pub.status = previous
                pub.error = stored_error(failure)
                db.commit()
        await run_in_threadpool(restore)
        if guidance:
            raise
        raise HTTPException(503, '公開要求を確認できません。状態を更新してください。') from None
    def event():
        with request.app.state.sessions() as db:
            db.add(PublicationEvent(project_id=project.id, build_id=row.id, actor_id=user.id, action=action))
            audit_action = 'publication.requested' if action == 'publish' else 'publication.start_requested'
            db.add(Audit(actor_id=user.id, action=audit_action, resource_id=project.id, detail=row.id))
            db.commit()
    await run_in_threadpool(event)
    return result


@router.delete('/api/projects/{project_id}/publication')
async def stop(project_id: UUID, request: Request):
    with request.app.state.sessions() as db:
        project, user = operational(db, request, project_id, mutation=True)
    result = await call(request.app.state.settings, 'DELETE', f'/projects/{project.id}')
    def save():
        with request.app.state.sessions() as db:
            pub = db.get(AppPublication, project.id)
            if pub:
                pub.status = 'stopped'
            db.add(PublicationEvent(project_id=project.id, actor_id=user.id, action='stop'))
            db.add(Audit(actor_id=user.id, action='publication.stopped', resource_id=project.id))
            db.commit()
    await run_in_threadpool(save)
    return result or {'status': 'stopped'}


class GrantInput(BaseModel):
    users: list[UUID] = Field(default_factory=list, max_length=500)
    departments: list[UUID] = Field(default_factory=list, max_length=100)


@router.get('/api/projects/{project_id}/publication/grants')
def grants(project_id: UUID, request: Request):
    with request.app.state.sessions() as db:
        project, user = operational(db, request, project_id)
        saved = list(db.scalars(select(PublicationGrant).where(PublicationGrant.project_id == project.id)))
        users = list(db.scalars(select(User).join(UserTenant, User.id == UserTenant.user_id).where(
            UserTenant.tenant_id == project.tenant_id, User.enabled.is_(True))))
        # Departments are global; actual use still requires current tenant membership.
        return {'users': [g.subject_id for g in saved if g.kind == 'user'],
            'departments': [g.subject_id for g in saved if g.kind == 'department'],
            'available_users': [{'id': u.id, 'name': u.display_name or u.email} for u in users
                if 'user' in tenant_role_set(db, u, project.tenant_id)],
            'available_departments': [{'id': d.id, 'name': d.name} for d in db.scalars(
                select(Department).where(Department.enabled.is_(True)))]}


@router.put('/api/projects/{project_id}/publication/grants')
def save_grants(project_id: UUID, payload: GrantInput, request: Request):
    with request.app.state.sessions() as db:
        project, user = operational(db, request, project_id, mutation=True)
        for uid in payload.users:
            target = db.get(User, str(uid))
            if (not target or not target.enabled or not tenant_member(db, uid, project.tenant_id)
                    or 'user' not in tenant_role_set(db, target, project.tenant_id)):
                raise HTTPException(422, '同じテナントの有効な利用者を選んでください。')
        for did in payload.departments:
            target = db.get(Department, str(did))
            if not target or not target.enabled:
                raise HTTPException(422, '有効な部門を選んでください。')
        db.execute(delete(PublicationGrant).where(PublicationGrant.project_id == project.id))
        for kind, subjects in (('user', payload.users), ('department', payload.departments)):
            db.add_all([PublicationGrant(project_id=project.id, kind=kind, subject_id=str(s)) for s in set(subjects)])
        db.add(Audit(actor_id=user.id, action='publication.grants_updated', resource_id=project.id))
        db.commit()
    return {'saved': True}


@router.get('/api/publication-operations')
def operation_list(request: Request):
    with request.app.state.sessions() as db:
        user = actor(request, db)
        allowed = operator_tenant_ids(db, user)
        if not allowed:
            return []
        rows = db.execute(select(Project, AppPublication, Tenant).join(
            AppPublication, Project.id == AppPublication.project_id).join(
            Tenant, Tenant.id == Project.tenant_id).where(
            Project.tenant_id.in_(allowed))
            .order_by(Tenant.name, Project.name))
        result = []
        for project, pub, tenant in rows:
            candidate = registered_build(db, project.id, pub)
            if candidate is None:
                continue
            result.append({'id': project.id, 'name': project.name, 'tenant_id': tenant.id,
                'tenant_name': tenant.name, 'status': pub.status,
                'build_id': pub.build_id or candidate.id,
                'revision': candidate.revision,
                'build_created_at': candidate.created_at.replace(tzinfo=timezone.utc)
                    if candidate.created_at.tzinfo is None else candidate.created_at,
                'candidate_build_id': candidate.id,
                'candidate_revision': candidate.revision,
                'startable': pub.status in ('stopped', 'failed'),
                'error': pub.error,
                'updated_at': pub.updated_at})
        return result


@router.get('/api/publication-operations/{project_id}/versions')
def operation_versions(project_id: UUID, request: Request):
    with request.app.state.sessions() as db:
        project, _ = operational(db, request, project_id)
        pub = db.get(AppPublication, project.id)
        if not registered_build(db, project.id, pub):
            return []
        return [{'id': build.id, 'revision': build.revision, 'created_at': build.created_at,
                 'digest': build.digest} for build in db.scalars(select(AppBuild).where(
                 AppBuild.project_id == project.id, AppBuild.status == 'succeeded',
                 AppBuild.digest.is_not(None), or_(AppBuild.id == pub.build_id,
                     AppBuild.id.in_(select(PublicationEvent.build_id).where(
                         PublicationEvent.project_id == project.id,
                         PublicationEvent.action.in_(RELEASE_ACTIONS)))))
                 .order_by(AppBuild.created_at.desc()))]


@router.get('/api/publication-operations/{project_id}/resources')
async def operation_resources(project_id: UUID, request: Request):
    with request.app.state.sessions() as db:
        project, _ = operational(db, request, project_id)
        pub = db.get(AppPublication, project.id)
        if not pub:
            raise HTTPException(404, '公開アプリが見つかりません。')
        saved = pub.resources or {}
    info = await call(request.app.state.settings, 'GET', f'/projects/{project_id}/resources')
    if info is None:
        raise HTTPException(503, '公開基盤のリソース設定を確認できません。')
    return {'values': {**info['defaults'], **saved}, 'pvc_size': info['pvc_size'],
            'expandable': info['expandable'], 'min_storage_gi': info['min_storage_gi']}


@router.put('/api/publication-operations/{project_id}/resources')
async def update_operation_resources(project_id: UUID, payload: PublicationResources, request: Request):
    with request.app.state.sessions() as db:
        project, _ = operational(db, request, project_id, mutation=True)
        publication = db.get(AppPublication, project.id)
        if publication is None:
            raise HTTPException(404, '公開アプリが見つかりません。')
        saved = dict(publication.resources or {})
    info = await call(request.app.state.settings, 'GET', f'/projects/{project_id}/resources')
    if info is None:
        raise HTTPException(503, '公開基盤のリソース設定を確認できません。')
    if payload.storage_gi < info['min_storage_gi']:
        raise HTTPException(409, f'PVC容量は{info["min_storage_gi"]}Gi以上を指定してください。')
    current = info['pvc_size']
    current_gi = None
    if current:
        if not current.endswith('Gi') or not current[:-2].isdigit():
            raise HTTPException(409, '既存PVCの容量を確認できません。管理者に確認してください。')
        current_gi = int(current[:-2])
        if payload.storage_gi < current_gi:
            raise HTTPException(409, 'PVC容量は縮小できません。現在の容量以上を指定してください。')
        if payload.storage_gi > current_gi and not info['expandable']:
            raise HTTPException(409, 'このStorageClassはPVCの容量拡張に対応していません。')
    for value, previous, minimum, step, label in (
        (payload.cpu_request_m, saved.get('cpu_request_m'), 50, 50, 'CPU要求量'),
        (payload.memory_request_mi, saved.get('memory_request_mi'), 256, 256, 'メモリ要求量'),
        (payload.memory_limit_mi, saved.get('memory_limit_mi'), 256, 256, 'メモリ上限'),
        (payload.storage_gi, saved.get('storage_gi'), 5, 5, 'PVC容量'),
    ):
        unchanged_existing_pvc = label == 'PVC容量' and value == current_gi
        if (value < minimum or value % step) and value != previous and not unchanged_existing_pvc:
            raise HTTPException(422, f'{label}は{minimum}以上、{step}単位で指定してください。')
    with request.app.state.sessions() as db:
        project, user = operational(db, request, project_id, mutation=True)
        pub = db.get(AppPublication, project.id, with_for_update=True)
        if pub.status in ('starting', 'updating', 'stopping', 'deleting'):
            raise HTTPException(409, '公開操作中はリソースを変更できません。')
        pub.resources = payload.model_dump()
        db.add(Audit(actor_id=user.id, action='publication.resources_updated',
                     resource_id=project.id, detail=json.dumps(pub.resources, sort_keys=True)))
        db.commit()
    return {'values': payload.model_dump(), 'pvc_size': current,
            'expandable': info['expandable'], 'min_storage_gi': info['min_storage_gi']}


@router.post('/api/publication-operations/{project_id}/start', status_code=202)
async def operation_start(project_id: UUID, request: Request):
    def selected():
        with request.app.state.sessions() as db:
            project, user = operational(db, request, project_id, mutation=True)
            pub = db.get(AppPublication, project.id)
            build = registered_build(db, project.id, pub)
            if not build:
                raise HTTPException(409, '先に公開タブでビルド済みの版を公開してください。')
            if pub.status not in ('stopped', 'failed'):
                raise HTTPException(409, '公開操作中、または既に起動しています。')
            return project, user, build
    project, user, build = await run_in_threadpool(selected)
    return await publish_build(request, project, user, build, action='start')


@router.post('/api/publication-operations/{project_id}/stop')
async def operation_stop(project_id: UUID, request: Request):
    return await stop(project_id, request)


@router.delete('/api/publication-operations/{project_id}')
async def operation_delete(project_id: UUID, request: Request):
    with request.app.state.sessions() as db:
        project, user = operational(db, request, project_id, mutation=True)
    await sync(request, project)
    with request.app.state.sessions() as db:
        pub = db.get(AppPublication, project.id)
        if not pub:
            raise HTTPException(404, '公開アプリが見つかりません。')
        if pub.status not in ('stopped', 'deleting'):
            raise HTTPException(409, '公開アプリを停止してから削除してください。')
    result = await call(request.app.state.settings, 'DELETE', f'/projects/{project.id}/data')
    if result is None or result.get('status') != 'deleted':
        raise HTTPException(503, '公開データの削除を確認できません。再試行してください。')
    with request.app.state.sessions() as db:
        pub = db.get(AppPublication, project.id, with_for_update=True)
        if pub:
            db.execute(delete(PublicationGrant).where(PublicationGrant.project_id == project.id))
            db.delete(pub)
        db.add(PublicationEvent(project_id=project.id, actor_id=user.id, action='delete'))
        db.add(Audit(actor_id=user.id, action='publication.deleted', resource_id=project.id))
        db.commit()
    return {'status': 'deleted'}


@router.get('/api/publication-operations/{project_id}/grants')
def operation_grants(project_id: UUID, request: Request):
    return grants(project_id, request)


@router.put('/api/publication-operations/{project_id}/grants')
def operation_save_grants(project_id: UUID, payload: GrantInput, request: Request):
    return save_grants(project_id, payload, request)


@router.get('/api/publication-operations/{project_id}/runtime')
async def operation_runtime(project_id: UUID, request: Request):
    with request.app.state.sessions() as db:
        operational(db, request, project_id)
    if (getattr(request.app.state.settings, 'app_env', None) == 'local'
            and getattr(request.app.state.settings, 'publication_controller_url', None) == 'http://publication-controller:8080'):
        return await call(request.app.state.settings, 'GET', f'/projects/{project_id}/runtime')
    if not cluster_reader.available():
        return {'available': False, 'pods': [], 'deployment': None}
    namespace = request.app.state.settings.app_name + '-published'
    name = 'published-' + str(project_id)
    try:
        state = await cluster_reader.read_published(namespace, name)
    except Exception:
        raise HTTPException(503, '公開Podの状態を取得できません。') from None
    return {'available': True, **state}


@router.get('/api/publication-operations/{project_id}/pods/{pod}/logs')
async def operation_pod_logs(project_id: UUID, pod: str, request: Request):
    runtime = await operation_runtime(project_id, request)
    if not runtime['available'] or pod not in {item['name'] for item in runtime['pods']}:
        raise HTTPException(404, '公開Podが見つかりません。')
    if runtime.get('kind') == 'docker':
        return await call(request.app.state.settings, 'GET', f'/projects/{project_id}/logs')
    namespace = request.app.state.settings.app_name + '-published'
    try:
        return await cluster_reader.read_logs(namespace, pod, namespaces=(namespace,))
    except (ValueError, LookupError):
        raise HTTPException(404, '公開Podが見つかりません。') from None
    except Exception:
        raise HTTPException(503, 'ログを取得できません。') from None


class EnvironmentInput(BaseModel):
    entries: list[dict] = Field(max_length=50)


@router.get('/api/publication-operations/{project_id}/environment')
@router.get('/api/projects/{project_id}/publication/environment')
def get_environment(project_id: UUID, request: Request):
    with request.app.state.sessions() as db:
        project, _ = operational(db, request, project_id)
        pub = db.get(AppPublication, project.id)
        return {'entries': preview_env.visible(pub.environment if pub else [])}


@router.put('/api/publication-operations/{project_id}/environment')
@router.put('/api/projects/{project_id}/publication/environment')
def save_environment(project_id: UUID, payload: EnvironmentInput, request: Request):
    try:
        with request.app.state.sessions() as db:
            project, user = operational(db, request, project_id, mutation=True)
            incoming = preview_env.parse(payload.entries)
            key = request.app.state.settings.tenant_secret_key.get_secret_value()
            pub = db.get(AppPublication, project.id, with_for_update=True)
            if pub and pub.status in ('starting', 'updating', 'stopping'):
                raise HTTPException(409, '公開操作中です。完了後に環境変数を保存してください。')
            if not pub:
                pub = AppPublication(project_id=project.id)
                db.add(pub)
            saved = {e['name']: e for e in (pub.environment or [])}
            entries = []
            for entry in incoming:
                value = entry['value']
                if value is None:
                    old = saved.get(entry['name'])
                    if not old or not old['secret']:
                        raise preview_env.InvalidEnvironment('秘密の値を入力してください。')
                    value = old['value']
                elif entry['secret']:
                    value = seal(key, value)
                entries.append({**entry, 'value': value})
            pub.environment = entries
            db.add(Audit(actor_id=user.id, action='publication.environment_updated', resource_id=project.id))
            db.commit()
        return {'entries': preview_env.visible(entries)}
    except (preview_env.InvalidEnvironment, SecretBoxUnavailable) as exc:
        raise HTTPException(422, str(exc)) from None


@router.get('/api/published-apps')
def catalog(request: Request):
    with request.app.state.sessions() as db:
        user = actor(request, db)
        return [{'id': p.id, 'name': p.name, 'purpose': p.purpose,
                 'url': url_of(request.app.state.settings, p.id, 'published'),
                 'tenant_id': t.id, 'tenant_name': t.name}
            for p, pub, t in db.execute(select(Project, AppPublication, Tenant).join(AppPublication,
                Project.id == AppPublication.project_id).join(Tenant, Tenant.id == Project.tenant_id)
                .where(AppPublication.status == 'running').order_by(Tenant.name, Project.name))
            if permitted(db, p, user, pub)]


async def reconcile_publications(app):
    """Mirror controller state without requiring a developer to keep the screen open."""
    import asyncio
    import logging
    from types import SimpleNamespace
    request = SimpleNamespace(app=app)
    while True:
        if app.state.settings.publication_enabled:
            def projects():
                with app.state.sessions() as db:
                    return list(db.scalars(select(Project).where(
                        Project.id.in_(select(AppPublication.project_id)) | Project.id.in_(
                        select(AppBuild.project_id).where(AppBuild.status.in_(ACTIVE_BUILDS))))))
            try:
                for project in await run_in_threadpool(projects):
                    try:
                        await sync(request, project)
                    except Exception:
                        logging.getLogger('koyorina.publication').warning('State synchronization failed; retrying')
            except Exception:
                logging.getLogger('koyorina.publication').warning('Publication database unavailable; retrying')
        await asyncio.sleep(5)
