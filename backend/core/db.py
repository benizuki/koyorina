from datetime import datetime, timezone
from uuid import uuid4
from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, String, Text, JSON, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker


def now():
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Department(Base):
    """所属組織のマスター。利用者の所属を選ぶための一覧。"""
    __tablename__ = "departments"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    name: Mapped[str] = mapped_column(String(80), unique=True)
    note: Mapped[str] = mapped_column(String(200), default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class Tenant(Base):
    """Physical isolation boundary for a group company or strictly separated organization."""
    __tablename__ = "tenants"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    name: Mapped[str] = mapped_column(String(80), unique=True)
    note: Mapped[str] = mapped_column(String(200), default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class SystemSetting(Base):
    """画面から変えるシステム全体の設定。秘密は暗号化した値だけをJSONへ保存する。"""
    __tablename__ = "system_settings"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class TenantLlmSetting(Base):
    """テナントの生成AI（生成に使う Gemini・Antigravity・OpenAI互換・Claude）。

    値の形はシステム設定（SystemSetting）と同じ。キーは暗号化して入る。
    行が無い種類はシステムの既定を使う。{"disabled": true} はそのテナントでは使わせない。
    """
    __tablename__ = "tenant_llm_settings"
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), primary_key=True)
    kind: Mapped[str] = mapped_column(String(40), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class TenantAiSettings(Base):
    """テナントの生成アプリが使うGeminiの接続先。アプリ側の環境変数で上書きできる既定値。

    APIキーは暗号化して持つ（core/secret_box）。Vertex AI はWorkload Identity連携で
    認証するので、鍵もトークンも保存しない。WIFの識別子は秘密ではない。
    """
    __tablename__ = "tenant_ai_settings"
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"),
                                           primary_key=True)
    backend: Mapped[str] = mapped_column(String(20), default="none")
    api_key_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    gcp_project: Mapped[str] = mapped_column(String(64), default="")
    location: Mapped[str] = mapped_column(String(64), default="")
    model: Mapped[str] = mapped_column(String(100), default="")
    thinking_level: Mapped[str] = mapped_column(String(20), default="")
    wif_project_number: Mapped[str] = mapped_column(String(20), default="")
    wif_pool_id: Mapped[str] = mapped_column(String(64), default="")
    wif_provider_id: Mapped[str] = mapped_column(String(64), default="")
    wif_service_account: Mapped[str] = mapped_column(String(200), default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class UserTenant(Base):
    """利用者はいくつのテナントにも属せる。身元はグループで1つなので、行を足すだけでよい。"""
    __tablename__ = "user_tenants"
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"),
                                         primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"),
                                           primary_key=True, index=True)
    # Legacy primary role. New authorization uses UserTenantRole.
    role: Mapped[str] = mapped_column(String(20), default="user", server_default="user")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class UserTenantRole(Base):
    """Independent roles assigned within one tenant."""
    __tablename__ = "user_tenant_roles"
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), primary_key=True)
    role: Mapped[str] = mapped_column(String(20), primary_key=True)


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    email: Mapped[str] = mapped_column(String(320), unique=True)
    display_name: Mapped[str] = mapped_column(String(80), default="")
    # 同じメールアドレスの別アカウントを弾くための照合先。初回ログインで記録する。
    google_subject: Mapped[str | None] = mapped_column(String(64), nullable=True)
    department_id: Mapped[str | None] = mapped_column(ForeignKey("departments.id"), nullable=True, index=True)
    # システムロール。admin=全テナントを管理 / member=テナントごとのロール（UserTenant.role）に従う
    role: Mapped[str] = mapped_column(String(20), default="member")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # 本人のChatGPT枠を使うCodexだけを利用者単位で停止できる。Geminiは引き続き利用できる。
    codex_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class Project(Base):
    __tablename__ = "projects"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    # The schema makes this NOT NULL. The default keeps direct ORM construction (imports and
    # fixtures) in the same deterministic legacy tenant; API creation still validates membership.
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), nullable=False, index=True,
                                            default="00000000-0000-4000-8000-000000000001")
    name: Mapped[str] = mapped_column(String(80))
    purpose: Mapped[str] = mapped_column(Text)
    audience: Mapped[str] = mapped_column(String(30))
    fields: Mapped[list] = mapped_column(JSON)
    # 複数の帳票・業務テーブル・マスター。fieldsは先頭の業務テーブルとの互換用に残す。
    tables: Mapped[list | None] = mapped_column(JSON, nullable=True)
    requirements: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # 仕様から作った初期文を利用者が直した場合、その依頼文を生成まで保持する。
    generation_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 作成時に選んだ業務区分・可視化目的と、サンプルデータの列割り当て。
    creation_profile: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="draft")
    revision: Mapped[int] = mapped_column(Integer, default=1)
    approved_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # プレビューへ渡す利用者指定の環境変数。[{name, value, secret}]
    preview_env: Mapped[list | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class ProjectCollaborator(Base):
    """アプリを一緒に触る人。削除だけは持たないので、役割の列は置かない。"""
    __tablename__ = "project_collaborators"
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"),
                                            primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"),
                                         primary_key=True, index=True)
    added_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ProjectSession(Base):
    """いま開発している人。アプリごとに1人だけ持てる。"""
    __tablename__ = "project_sessions"
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"),
                                            primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Audit(Base):
    __tablename__ = "audit_events"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    actor_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    action: Mapped[str] = mapped_column(String(80))
    resource_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    # 何をしたか。操作の種類だけでは足りないものにだけ入れる（実行したコマンドなど）。
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class SupportSession(Base):
    """Short-lived, reason-bound platform administrator access to one tenant."""
    __tablename__ = "support_sessions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    actor_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    permission: Mapped[str] = mapped_column(String(10))
    reason: Mapped[str] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class TenantMigration(Base):
    """Auditable project move between isolated tenant storage claims."""
    __tablename__ = "tenant_migrations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    source_tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    target_tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    actor_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    reason: Mapped[str] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_retained_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class StorageUsageSnapshot(Base):
    """Last trusted measurement of one tenant's isolated storage claim."""
    __tablename__ = "storage_usage_snapshots"
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"),
                                           primary_key=True)
    kind: Mapped[str] = mapped_column(String(20), primary_key=True)
    claim_name: Mapped[str] = mapped_column(String(253))
    requested_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    used_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    capacity_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    available_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    status: Mapped[str] = mapped_column(String(20), default="not_measured")
    error: Mapped[str | None] = mapped_column(String(300), nullable=True)
    measured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class GenerationJob(Base):
    __tablename__ = "generation_jobs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    # 同じ目的の依頼をまとめる開発チャット。作業コードはプロジェクト内で共通。
    chat_id: Mapped[str] = mapped_column(String(36), default=lambda: str(uuid4()), index=True)
    revision: Mapped[int] = mapped_column(Integer)
    specification: Mapped[dict] = mapped_column(JSON)
    # 空なら承認済み仕様からの初回生成、値があれば既存コードへの変更依頼。
    instruction: Mapped[str | None] = mapped_column(Text, nullable=True)
    # この依頼に添えた資料の名前。次の依頼へ持ち越さないよう、ここに記録して切り離す。
    attachments: Mapped[list | None] = mapped_column(JSON, nullable=True)
    source_type: Mapped[str] = mapped_column(String(30), default="managed_codex")
    artifact: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="starting")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 生成AIの最後の報告（伏せ字済み）と、見送った機能。チャットの返事として出す。
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_steps: Mapped[list | None] = mapped_column(JSON, nullable=True)
    provider: Mapped[str | None] = mapped_column(String(20), nullable=True)
    model: Mapped[str | None] = mapped_column(String(80), nullable=True)
    effort: Mapped[str | None] = mapped_column(String(20), nullable=True)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cached_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


def database(url: str):
    # 画面の資産取得ごとに所有者確認が走るため、短時間の集中に耐える余裕を持たせる。
    # 接続待ちで詰まらせないよう待ち時間も区切る。
    engine = create_engine(url, pool_pre_ping=True, pool_size=10, max_overflow=10, pool_timeout=10)
    return engine, sessionmaker(engine, expire_on_commit=False)


class AppBuild(Base):
    __tablename__ = 'app_builds'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    project_id: Mapped[str] = mapped_column(ForeignKey('projects.id'), index=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey('tenants.id'))
    generation_id: Mapped[str] = mapped_column(String(36))  # survives generation history reset
    revision: Mapped[int] = mapped_column(Integer)
    actor_id: Mapped[str] = mapped_column(ForeignKey('users.id'))
    source_hash: Mapped[str] = mapped_column(String(64))
    registry_kind: Mapped[str] = mapped_column(String(20))
    image: Mapped[str] = mapped_column(String(500))
    digest: Mapped[str | None] = mapped_column(String(80), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default='queued')
    error: Mapped[str | None] = mapped_column(String(300), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class AppPublication(Base):
    __tablename__ = 'app_publications'
    project_id: Mapped[str] = mapped_column(ForeignKey('projects.id'), primary_key=True)
    build_id: Mapped[str | None] = mapped_column(ForeignKey('app_builds.id'), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default='stopped')
    error: Mapped[str | None] = mapped_column(String(300), nullable=True)
    environment: Mapped[list] = mapped_column(JSON, default=list)
    resources: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class PublicationGrant(Base):
    __tablename__ = 'publication_grants'
    project_id: Mapped[str] = mapped_column(ForeignKey('projects.id'), primary_key=True)
    kind: Mapped[str] = mapped_column(String(20), primary_key=True)
    subject_id: Mapped[str] = mapped_column(String(36), primary_key=True)


class PublicationEvent(Base):
    __tablename__ = 'publication_events'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    project_id: Mapped[str] = mapped_column(ForeignKey('projects.id'), index=True)
    build_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    actor_id: Mapped[str] = mapped_column(ForeignKey('users.id'))
    action: Mapped[str] = mapped_column(String(30))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
