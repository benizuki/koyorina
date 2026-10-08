"""生成アプリを配信するホスト名と、そこへ本人を引き渡すための署名。

生成アプリはKoyorinaとは別のオリジンで動かす。同じオリジンに置くと、アプリの
JavaScriptが閲覧者のKoyorinaセッションで管理APIを呼べてしまう（開いた人の権限を
借りられる）。ホストはプロジェクトごと・プレビューと公開版ごとに分ける。

    プレビュー: <project_id>-dev.<suffix>
    公開版:     <project_id>.<suffix>

suffix は既定でKoyorina自身のホスト名。ワイルドカード証明書1枚（*.<suffix>）で
両方まかなえるよう、階層は1つに収める。パスは今までどおり /apps/<id>/ と
/published-apps/<id>/ のまま使う（公開版はビルド時にベースパスを焼き込んでいるため）。
"""
import ipaddress
import re
from urllib.parse import urlsplit
from uuid import UUID
from itsdangerous import BadSignature, URLSafeTimedSerializer

KINDS = ("preview", "published")
PREVIEW_MARK = "-dev"
# 本人の引き渡しに使う一時トークンの寿命。リダイレクト1往復で使い切る。
HANDOFF_MAX_AGE = 60
# アプリ側ホストのセッション。Koyorina本体のセッションと揃える。
SESSION_MAX_AGE = 28800
HANDOFF_PATH = "/__koyorina/handoff"
_HOST = re.compile(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})(-dev)?")


def host_suffix(app_origin: str, configured: str = "") -> str:
    """アプリ用ホストの親ドメイン。IPアドレスにはサブドメインを付けられないので localhost にする。"""
    if configured:
        return configured.strip().strip(".").lower()
    hostname = (urlsplit(app_origin).hostname or "").lower()
    try:
        ipaddress.ip_address(hostname)
        return "localhost"
    except ValueError:
        return hostname


def app_host(project_id, kind: str, suffix: str) -> str:
    if kind not in KINDS:
        raise ValueError("unknown app kind")
    mark = PREVIEW_MARK if kind == "preview" else ""
    return f"{UUID(str(project_id))}{mark}.{suffix}"


def app_origin(settings_origin: str, project_id, kind: str, suffix: str) -> str:
    """生成アプリのオリジン。スキームとポートはKoyorina本体に揃える。"""
    origin = urlsplit(settings_origin)
    port = f":{origin.port}" if origin.port else ""
    return f"{origin.scheme}://{app_host(project_id, kind, suffix)}{port}"


def parse_host(host: str, suffix: str):
    """Hostヘッダーから (project_id, kind) を取り出す。アプリ用ホストでなければ None。"""
    hostname = urlsplit(f"//{host}").hostname or ""
    label, dot, rest = hostname.lower().partition(".")
    if not dot or rest != suffix:
        return None
    matched = _HOST.fullmatch(label)
    if not matched:
        return None
    return UUID(matched.group(1)), "preview" if matched.group(2) else "published"


def wildcard_source(settings_origin: str, suffix: str) -> str:
    """CSPのframe-srcに書く形（プレビューを管理画面のiframeへ埋め込むため）。"""
    origin = urlsplit(settings_origin)
    port = f":{origin.port}" if origin.port else ""
    return f"{origin.scheme}://*.{suffix}{port}"


def _serializer(secret: str, purpose: str) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(secret or "koyorina-local", salt="koyorina-app-" + purpose)


def sign(secret: str, purpose: str, user_id: str, project_id, kind: str) -> str:
    return _serializer(secret, purpose).dumps({"u": str(user_id), "p": str(UUID(str(project_id))),
                                               "k": kind})


def verify(secret: str, purpose: str, token: str, project_id, kind: str, max_age: int):
    """署名・期限・対象（プロジェクトと種別）が合うときだけ利用者IDを返す。"""
    if not token:
        return None
    try:
        data = _serializer(secret, purpose).loads(token, max_age=max_age)
    except BadSignature:
        return None
    if (not isinstance(data, dict) or data.get("p") != str(UUID(str(project_id)))
            or data.get("k") != kind or not isinstance(data.get("u"), str)):
        return None
    return data["u"]


def safe_next(path: str, base: str) -> str:
    """戻り先はそのアプリのベースパス配下だけ。外部URLや別アプリへは戻さない。"""
    if (isinstance(path, str) and path.startswith(base) and "\\" not in path
            and not any(ch in path for ch in "\r\n") and "//" not in path):
        return path
    return base
