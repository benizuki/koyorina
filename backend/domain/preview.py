"""生成コードをディスクへ展開し、実行コンテナと共有するための純粋処理。

生成物は信頼しない。パスは CodeBundle と同じ規則で再検証し、
ワークスペースの外へ書き出さない。秘密情報はここで扱わない。
"""
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import os
import re
import shutil
from pathlib import Path
from uuid import UUID
from backend.domain.generation import IGNORED_GENERATED_PARTS, CodeBundle, artifact_path_is_allowed

STATES = ("stopped", "starting", "running", "failed")


class PreviewPaths:
    def __init__(self, root: Path, project_id):
        # UUID以外をディレクトリ名にしない。呼び出し側の入力を信用しない。
        self.base = root.resolve() / str(UUID(str(project_id)))
        self.workspace = self.base / "workspace"
        self.var = self.base / "var"
        self.state_file = self.base / "state.json"

    def prepare(self):
        self.workspace.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.var.mkdir(mode=0o700, parents=True, exist_ok=True)
        return self


# 起動に見込む時間。依存の導入と画面ビルドで数分かかる。ここを過ぎても応答が無ければ、
# 待ち続けても始まらないものとして扱う。k8s側のstartupProbeは10分で諦めるので、
# それより後に来るように取る。
STARTUP_BUDGET = timedelta(minutes=15)


def startup_expired(state: dict, now=None) -> bool:
    """起動を待ち続ける時間を過ぎたか。

    「まだ応答が無い」ことは失敗の証拠ではない。証拠が無いうちに異常終了と出すと、
    順調に起動している最中の画面に赤い札が出る。ただし待つ時間には上限を置く。
    上限が無いと、載せられないPodをいつまでも「起動中」と表示し続けることになる。
    """
    started = state.get("updated_at")
    if not started:
        return False
    try:
        at = datetime.fromisoformat(started)
    except ValueError:
        return False
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    return (now or datetime.now(timezone.utc)) - at > STARTUP_BUDGET


def read_state(paths: PreviewPaths) -> dict:
    if not paths.state_file.is_file():
        return {}
    try:
        state = json.loads(paths.state_file.read_text())
    except (ValueError, OSError):
        return {}
    return state if isinstance(state, dict) else {}


def write_state(paths: PreviewPaths, state: dict):
    paths.base.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = paths.base / "state.tmp"
    temporary.write_text(json.dumps(state))
    temporary.replace(paths.state_file)


def allocate_port(root: Path, project_id, base: int, count: int) -> int:
    """固定枠から割り当てる。枠はGoogle OAuthの生成元登録を有限にするための制約。"""
    paths = PreviewPaths(root, project_id)
    own = read_state(paths).get("port")
    allowed = range(base, base + count)
    if own in allowed:
        return own
    taken = set()
    for candidate in sorted(p for p in root.resolve().glob("*/state.json") if p.is_file()):
        if candidate.parent.name == paths.base.name:
            continue
        try:
            taken.add(json.loads(candidate.read_text()).get("port"))
        except (ValueError, OSError):
            continue
    for port in allowed:
        if port not in taken:
            return port
    raise ValueError("同時に実行できるプレビューの上限に達しています。")


def clear_sources(workspace: Path):
    """前回展開した生成コードだけを消す。依存物と var/ は残す。"""
    root = workspace.resolve()
    for directory, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        base = Path(directory)
        for name in tuple(dirnames):
            if (base / name).is_symlink():
                (base / name).unlink()
                dirnames.remove(name)
            elif name in IGNORED_GENERATED_PARTS:
                dirnames.remove(name)
        for name in filenames:
            (base / name).unlink()
    for directory, _, _ in os.walk(root, topdown=False, followlinks=False):
        base = Path(directory)
        if base != root and base.name not in IGNORED_GENERATED_PARTS and not any(base.iterdir()):
            base.rmdir()


def materialize(workspace: Path, bundle: CodeBundle) -> str:
    """バンドルをワークスペースへ展開し、内容のダイジェストを返す。"""
    root = workspace.resolve(strict=True)
    for file in bundle.files:
        if not artifact_path_is_allowed(file.path):
            raise ValueError("許可されていない成果物のパスです。")
        target = (root / file.path).resolve()
        if not target.is_relative_to(root):
            raise ValueError("許可されていない成果物のパスです。")
    clear_sources(root)
    digest = hashlib.sha256()
    for file in sorted(bundle.files, key=lambda f: f.path):
        target = root / file.path
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        target.write_text(file.content, encoding="utf-8")
        digest.update(file.path.encode() + b"\0" + file.content.encode() + b"\0")
    return digest.hexdigest()


def dependency_digest(workspace: Path) -> str:
    digest = hashlib.sha256()
    # pyproject.toml が依存の宣言。requirements.txt は以前の規約のアプリぶん。
    for relative in ("pyproject.toml", "backend/requirements.txt", "frontend/package.json"):
        candidate = workspace / relative
        digest.update(relative.encode() + b"\0")
        if candidate.is_file() and not candidate.is_symlink():
            digest.update(candidate.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


# 依存の取得元。生成アプリの npm / uv はここを通す。
# 値を持つものだけを渡すが、名前は常に予約する（未設定の環境で、画面から
# 取得元を差し替えられてしまうことがないように）。
PACKAGE_SOURCE = ("NPM_CONFIG_REGISTRY", "NPM_CONFIG_MIN_RELEASE_AGE", "UV_DEFAULT_INDEX")


def package_source(npm_registry: str = "", npm_min_release_age: str = "",
                   pypi_index: str = "") -> dict:
    """取得元を検査済みの経路へ向ける。空なら何も渡さず、既定の公開レジストリを使う。

    uv は UV_DEFAULT_INDEX を読む（PIP_INDEX_URL は読まない）。npm は
    NPM_CONFIG_* を読む。どちらも設定ファイルを書かずに環境変数だけで足りる。
    """
    values = {"NPM_CONFIG_REGISTRY": npm_registry,
              "NPM_CONFIG_MIN_RELEASE_AGE": npm_min_release_age,
              "UV_DEFAULT_INDEX": pypi_index}
    return {name: str(value) for name, value in values.items() if value}


def runtime_environment(project_id, app_origin: str, session_secret: str, forward_secret: str,
                        google_client_id: str, admin_email: str, extra: dict | None = None,
                        packages: dict | None = None) -> dict:
    """生成アプリへ渡す設定。KoyorinaのDBやCodex認証情報は決して渡さない。

    app_origin はそのアプリ自身のオリジン（<id>-dev.<suffix>、domain/app_hosts）。
    Koyorina本体とは別のオリジンで、アプリはその配下のパス（APP_BASE_PATH）で動く。

    extra は画面から指定された分。**先に置いて後から上書きする**ので、
    ここで決める値が必ず勝つ。名前の検査は domain/preview_env が行うが、
    順序でも守っておく（検査を通らない経路が増えても壊れないように）。
    """
    return {
        **{str(k): str(v) for k, v in (extra or {}).items()},
        # /var/preview/db はPVCではなくノードローカルのemptyDir（RWX環境ではNFS上の
        # SQLiteはfcntlロックの不安があるため）。プレビューのDBは引き継がれない前提。
        "DATABASE_URL": "sqlite+pysqlite:////var/preview/db/preview.db",
        "APP_ORIGIN": app_origin,
        "APP_BASE_PATH": base_path(project_id),
        "APP_SESSION_SECRET": session_secret,
        # これが設定されている間、アプリはKoyorinaが確認した本人をそのまま使う。
        "APP_FORWARD_SECRET": forward_secret,
        "GOOGLE_OAUTH_CLIENT_ID": google_client_id,
        "BOOTSTRAP_ADMIN_EMAIL": admin_email,
        "FRONTEND_DIST": "/workspace/frontend/dist",
        "PREVIEW_VAR": "/var/preview",
        **(packages or {}),
    }


def remove_workspace(paths: PreviewPaths):
    if paths.base.is_dir():
        shutil.rmtree(paths.base)


APP_PREFIX = "/apps/"
HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
              "te", "trailer", "transfer-encoding", "upgrade", "content-encoding",
              "content-length", "host"}
# アプリ独自のヘッダー（CSRFトークンなど）を落とさないよう除外式にする。
DENIED_REQUEST_HEADERS = {"host", "cookie", "content-length", "x-forwarded-for", "x-forwarded-host",
                          "x-forwarded-proto", "x-real-ip"}
DROPPED_RESPONSE_HEADERS = {"x-frame-options", "strict-transport-security", "public-key-pins"}
# アプリ用ホストの本人確認Cookie（core/app_session）。生成アプリへは渡さない。
PLATFORM_COOKIES = {"koyorina_app", "__Host-koyorina_app"}


def base_path(project_id) -> str:
    return APP_PREFIX + str(UUID(str(project_id))) + "/"


def forward_secret(project_id, session_secret: str) -> str:
    """Koyorinaとアプリの間の合言葉。保存せず毎回導出する。

    Koyorina本体は共有環境で永続ディスクを持たないため、状態ファイルに置かない。
    セッション鍵を入れ替えると値が変わり、次回の起動でアプリ側と揃う。
    """
    key = (session_secret or "koyorina-local").encode()
    return hmac.new(key, b"preview-forward:" + UUID(str(project_id)).bytes, hashlib.sha256).hexdigest()


def resource_name(project_id) -> str:
    return "preview-" + UUID(str(project_id)).hex


def service_target(project_id, namespace: str = "koyorina-preview") -> str:
    """共有環境の転送先。名前はプロジェクトIDからのみ決まる。"""
    return f"http://{resource_name(project_id)}.{namespace}.svc:8080"


def cookie_prefix(project_id) -> str:
    # 同一オリジンに複数の生成アプリが載るため、Cookie名を衝突しないよう分離する。
    return "fa" + UUID(str(project_id)).hex[:12] + "_"


def request_cookies(raw: str, project_id, session_cookie: str) -> str:
    """Koyorina自身のセッションCookieは生成アプリへ渡さない。名前は元に戻す。"""
    prefix = cookie_prefix(project_id)
    forwarded = []
    for item in raw.split(";"):
        entry = item.strip()
        if not entry or "=" not in entry:
            continue
        name, _, value = entry.partition("=")
        name = name.strip()
        if name == session_cookie or name in PLATFORM_COOKIES:
            continue
        if re.fullmatch(r"fa[0-9a-f]{12}_.+", name):
            # 別の生成アプリのCookieは渡さない。自分のものは元の名前へ戻す。
            if not name.startswith(prefix):
                continue
            name = name[len(prefix):]
        forwarded.append(name + "=" + value)
    return "; ".join(forwarded)


def response_cookie(raw: str, project_id) -> str:
    """名前を分離し、有効パスをそのアプリの下だけに限定する。"""
    parts = [part.strip() for part in raw.split(";")]
    name, _, value = parts[0].partition("=")
    name = name.strip()
    for marker in ("__Host-", "__Secure-"):
        if name.startswith(marker):
            name = name[len(marker):]
    attributes = [cookie_prefix(project_id) + name + "=" + value, "Path=" + base_path(project_id)]
    for part in parts[1:]:
        if part.split("=")[0].strip().lower() not in {"path", "domain"}:
            attributes.append(part)
    return "; ".join(attributes)


def request_headers(items, project_id, session_cookie: str) -> dict:
    """アプリへ渡す要求ヘッダー。X-Forge-*は利用者から受け取らず、必ずこちらで作る。"""
    result = {}
    for key, value in items:
        lowered = key.lower()
        if lowered in DENIED_REQUEST_HEADERS or lowered in HOP_BY_HOP or lowered.startswith("x-forge-"):
            continue
        result[key] = value
    cookies = request_cookies(dict(items).get("cookie", ""), project_id, session_cookie)
    if cookies:
        result["Cookie"] = cookies
    return result


def identity_headers(secret: str, identity: dict) -> dict:
    """Koyorinaで確認済みの本人をアプリへ渡す。アプリ側で再ログインさせない。

    合言葉が一致しない限りアプリは信用してはいけない（生成プロンプトで規定）。
    利用者が送ったX-Forge-*は転送許可リストに無いため、ここへは到達しない。
    """
    if not secret:
        return {}
    email = str(identity.get("email", ""))
    if not re.fullmatch(r"[A-Za-z0-9._+%-]{1,254}@[A-Za-z0-9.-]{1,254}", email):
        raise ValueError("転送する本人情報の形式が不正です。")
    return {"X-Forge-Auth": secret, "X-Forge-User-Email": email,
            "X-Forge-User-Id": str(UUID(str(identity["id"]))),
            "X-Forge-User-Admin": "true" if identity.get("admin") else "false"}


def embeddable(policy: str, frame_ancestor: str) -> str:
    """アプリのCSPから埋め込み先の指定を外し、Koyorinaの画面だけを許す指定に置き換える。"""
    directives = [part.strip() for part in policy.split(";")
                  if part.strip() and not part.strip().lower().startswith("frame-ancestors")]
    return "; ".join(directives + [f"frame-ancestors {frame_ancestor}"])


def response_headers(headers, project_id, frame_ancestor: str) -> list[tuple[str, str]]:
    """埋め込み先と絶対パスの転送先だけを書き換える。アプリ自身の保護は残す。

    アプリは別オリジンなので、'self' のままではKoyorinaの画面に埋め込めない。
    逆にCSPを持たないアプリでも、Koyorina以外からは埋め込ませない。
    """
    base = base_path(project_id)
    result = []
    framed = False
    for key, value in headers:
        lowered = key.lower()
        if lowered in HOP_BY_HOP or lowered in DROPPED_RESPONSE_HEADERS:
            continue
        if lowered == "set-cookie":
            value = response_cookie(value, project_id)
        elif lowered == "content-security-policy":
            value, framed = embeddable(value, frame_ancestor), True
        elif lowered == "location" and value.startswith("/") and not value.startswith("//"):
            value = base.rstrip("/") + value
        result.append((key, value))
    if not framed:
        result.append(("content-security-policy", f"frame-ancestors {frame_ancestor}"))
    return result
