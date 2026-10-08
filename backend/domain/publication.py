"""Published images and source snapshots. No generated code runs in the management app."""
import hashlib
import io
import tarfile
from pathlib import PurePosixPath
from uuid import UUID
from pydantic import BaseModel, Field, model_validator
from backend.domain.generation import CodeBundle, is_generated_leftover

ACTIVE_BUILDS = ('queued', 'building', 'pushing')
DIGEST_PREFIX = 'KOYORINA_IMAGE_DIGEST='


class PublicationResources(BaseModel):
    """Per-app runtime limits. Storage is in GiB and may only grow after PVC creation."""
    cpu_request_m: int = Field(ge=50, le=4000)
    cpu_limit_m: int = Field(ge=100, le=8000)
    memory_request_mi: int = Field(ge=128, le=16384)
    memory_limit_mi: int = Field(ge=256, le=32768)
    storage_gi: int = Field(ge=1, le=1024)

    @model_validator(mode='after')
    def limits_cover_requests(self):
        if self.cpu_limit_m < self.cpu_request_m or self.memory_limit_mi < self.memory_request_mi:
            raise ValueError('CPU・メモリの上限は要求量以上にしてください。')
        return self

    def container_resources(self):
        return {'requests': {'cpu': f'{self.cpu_request_m}m', 'memory': f'{self.memory_request_mi}Mi'},
                'limits': {'cpu': f'{self.cpu_limit_m}m', 'memory': f'{self.memory_limit_mi}Mi'}}


def image_reference(host, project_id, build_id):
    return f'{host}/{UUID(str(project_id))}:{UUID(str(build_id))}'


def version_image_reference(host, project_id, revision):
    """Human-readable alias for the latest build of a generated revision."""
    if not isinstance(revision, int) or revision < 1:
        raise ValueError('revision must be a positive integer')
    return f'{host}/{UUID(str(project_id))}:rev{revision}'


def retention(builds, published_build_id, keep: int):
    """消してよいビルドと、消してはいけないダイジェストを決める。

    builds は id・status・digest・created_at を持つもの。残すのは
      - 公開中（公開予定を含む）のビルド
      - 実行中のビルド
      - 成功したビルドの新しい keep 件（切り戻し用）
      - 失敗・中止したビルドの新しい keep 件（原因を調べる用。イメージはほぼ持たない）
    ほかは消す。残すビルドと同じダイジェストのイメージは消せない（中身が同じ）。
    """
    newest = sorted(builds, key=lambda b: b.created_at, reverse=True)
    kept = {b.id for b in newest if b.status in ACTIVE_BUILDS or b.id == published_build_id}
    kept |= {b.id for b in [b for b in newest if b.status == 'succeeded'][:keep]}
    kept |= {b.id for b in [b for b in newest if b.status not in ('succeeded', *ACTIVE_BUILDS)][:keep]}
    remove = [b for b in newest if b.id not in kept]
    protected = {b.digest for b in newest if b.id in kept and b.digest}
    return remove, protected


def published_base(project_id):
    return f'/published-apps/{UUID(str(project_id))}/'


def snapshot(bundle: CodeBundle):
    """Only application source; exclude data, credentials, dotfiles and supplied build recipes."""
    result = []
    for file in bundle.files:
        path = PurePosixPath(file.path)
        if (is_generated_leftover(path.parts) or any(part.startswith('.') for part in path.parts)
                or any(part in {'var', 'data', 'tests', 'setup', 'Skills'} for part in path.parts)
                or path.name in {'Dockerfile', 'AGENTS.md'}
                or path.suffix in {'.sql', '.md', '.txt', '.yaml', '.yml'}):
            continue
        if path.parts[0] not in {'backend', 'frontend'} and file.path != 'pyproject.toml':
            continue
        result.append((file.path, file.content))
    result.sort()
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode='w:gz') as archive:
        for name, content in result:
            data = content.encode()
            info = tarfile.TarInfo(name)
            info.size, info.mode, info.mtime = len(data), 0o644, 0
            archive.addfile(info, io.BytesIO(data))
    data = output.getvalue()
    # gzip header timestamps differ; hash canonical source instead of compressed bytes.
    sha = hashlib.sha256()
    for name, content in result:
        sha.update(name.encode() + b'\0' + content.encode() + b'\0')
    return data, sha.hexdigest()


def permitted(db, project, user, publication):
    from sqlalchemy import select
    from backend.core.db import Department, PublicationGrant, Tenant, UserTenant
    from backend.domain.roles import tenant_role_set
    if (not user.enabled or db.get(UserTenant, (user.id, project.tenant_id)) is None
            or 'user' not in tenant_role_set(db, user, project.tenant_id)):
        return False
    tenant = db.get(Tenant, project.tenant_id)
    if tenant is not None and not tenant.enabled:
        return False
    grants = list(db.scalars(select(PublicationGrant).where(PublicationGrant.project_id == project.id)))
    if any(g.kind == 'user' and g.subject_id == user.id for g in grants):
        return True
    department = db.get(Department, user.department_id) if user.department_id else None
    return bool(department and department.enabled and any(
        g.kind == 'department' and g.subject_id == department.id for g in grants))


def published_secret(project_id, key):
    import hmac
    return hmac.new(key.encode(), b'published-forward:' + UUID(str(project_id)).bytes,
                    hashlib.sha256).hexdigest()
