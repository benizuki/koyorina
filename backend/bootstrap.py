"""マイグレーション後に初期管理者を明示的に登録する。"""
from sqlalchemy import select
from backend.config.settings import Settings
from backend.core.db import Tenant, User, UserTenant, database

# 初期マイグレーションが作る既定テナント。
DEFAULT_TENANT_ID = "00000000-0000-4000-8000-000000000001"


def ensure_admin(db, email):
    """初期管理者を登録し、どのテナントにも属していなければ既定テナントへ入れる。

    0015は「移行時点の既存ユーザー」だけを既定テナントへ寄せる。新規環境では
    移行の後にここで管理者を作るので、何もしないと所属なしのまま残り、
    テナント単位の一覧・利用量に現れない。所属が1つでもあれば手を付けない
    （管理画面で付け替えた所属を、再配備のたびに戻さないため）。
    """
    user = db.scalar(select(User).where(User.email == email))
    if not user:
        user = User(email=email, role="admin")
        db.add(user)
        db.flush()
    if db.get(Tenant, DEFAULT_TENANT_ID) and not db.scalar(
            select(UserTenant.tenant_id).where(UserTenant.user_id == user.id).limit(1)):
        db.add(UserTenant(user_id=user.id, tenant_id=DEFAULT_TENANT_ID, role="admin"))


def main():
    settings = Settings()
    email = "local-developer@example.invalid" if settings.app_env == "local" else settings.bootstrap_admin_email.strip().lower()
    if not email:
        raise SystemExit("BOOTSTRAP_ADMIN_EMAIL を設定してください。")
    engine, sessions = database(settings.database_url)
    with sessions.begin() as db:
        ensure_admin(db, email)
    engine.dispose()
    print("初期管理者の登録を確認しました。")


if __name__ == "__main__":
    main()
