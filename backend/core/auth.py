import hashlib
from fastapi import HTTPException, Request
from sqlalchemy import select
from backend.core.db import Audit, User


def get_db(request: Request):
    with request.app.state.sessions() as db:
        yield db


def actor(request: Request, db):
    settings = request.app.state.settings
    if settings.app_env == "local":
        # サーバーは127.0.0.1にバインドする。転送ヘッダーを認証根拠にしない。
        # Docker Compose版では、ホストの127.0.0.1へ来た接続がDocker網のゲートウェイから
        # 届く。そのアドレスだけを LOCAL_CLIENT_HOSTS で足す（プレビューは別の網に置く）。
        allowed = {"127.0.0.1", "::1", "testclient", *settings.local_client_host_set}
        if request.client is None or request.client.host not in allowed:
            raise HTTPException(403, "開発モードはこの端末からのみ利用できます。")
        email = "local-developer@example.invalid"
    else:
        fingerprint = hashlib.sha256(settings.google_oauth_client_id.encode()).hexdigest()
        if request.session.get("client") != fingerprint:
            raise HTTPException(401, "ログインしてください。")
        email = request.session.get("email")
    user = db.scalar(select(User).where(User.email == email, User.enabled.is_(True)))
    if user is None or (settings.app_env != "local" and request.session.get("user_id") != user.id):
        raise HTTPException(401, "利用登録がないか停止されています。管理者に確認してください。")
    return user


def identify(request: Request):
    """本人だけを短いセッションで確定する。

    実行基盤やCodexとの通信は数十秒かかることがある。その間DB接続を握ると
    プールが尽き、同期クエリがイベントループを塞ぐ。同期処理なので別スレッドで呼ぶ。
    """
    with request.app.state.sessions() as db:
        return actor(request, db).id


def audit(request: Request, actor_id, action, resource_id=None, detail=None):
    with request.app.state.sessions() as db:
        db.add(Audit(actor_id=actor_id, action=action, resource_id=resource_id,
                     detail=detail[:2000] if detail else None))
        db.commit()
