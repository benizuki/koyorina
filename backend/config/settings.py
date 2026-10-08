from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import SecretStr, model_validator
from typing import Literal
import ipaddress
import os
import re
from urllib.parse import urlsplit
from backend.domain import app_hosts, app_images

# 秘密をファイルで受け取る置き場（Docker Composeのsecretsが /run/secrets に置く）。
# 指定したときだけ読む。ファイル名は設定名（例: tenant_secret_key）。環境変数が優先する。
# 環境変数と違い、docker inspect や compose config に値が出ない。
SECRETS_DIR = os.environ.get("KOYORINA_SECRETS_DIR") or None

# Docker Compose版（compose.yaml）のcontroller。compose網の中のサービス名で届く。
COMPOSE_CODEX_CONTROLLER = "http://codex-controller:8080"
COMPOSE_PUBLICATION_CONTROLLER = "http://publication-controller:8080"


class Settings(BaseSettings):
    # 値は環境変数（k8sのSecret/ConfigMap、composeのenvironment/secrets）だけから受け取る。
    # 作業フォルダの .env は読まない。手元に残ったファイルが知らないうちに混ざるため。
    model_config = SettingsConfigDict(extra="ignore", secrets_dir=SECRETS_DIR)
    app_env: str = "production"
    # Kubernetesのリリース名。画面表示用のVITE_APP_NAMEとは別。
    app_name: str = "koyorina"
    database_url: str
    app_origin: str = "http://127.0.0.1:8080"
    # 生成アプリを配信するホストの親ドメイン（<id>.<ここ>、<id>-dev.<ここ>）。
    # 空ならAPP_ORIGINのホスト名。DNSと証明書は *.<ここ> を用意する。
    app_host_suffix: str = ""
    app_session_secret: str = ""
    google_oauth_client_id: str = ""
    bootstrap_admin_email: str = ""
    codex_binary: str = "codex"
    runtime_root: Path = Path(".runtime")
    codex_controller_url: str = ""
    codex_controller_token: SecretStr = SecretStr("")
    codex_enabled: bool = True
    # モデル選択肢の初期選択。無効・未接続のprovider（例: Codex未接続時にcodexのまま）
    # を指定した場合は、選択肢の並び順（Codexが先頭）へ黙って戻る。
    default_generator: Literal["codex", "gemini", "antigravity", "openai_compatible", "claude"] = "codex"
    pdf_extraction_enabled: bool = False
    # 公開サービスで開けておくと危ない口。既定は閉じ、使う環境でだけ明示して開ける。
    # プレビューの中で任意のコマンドを実行する調査用の口（実行管理のコマンド）。
    preview_shell_enabled: bool = False
    # 手元のCodexで作ったZIPを成果物として登録する口と、そのための作業パッケージ。
    # 生成の検査・履歴を通らないコードが、そのままプレビューへ載る。
    local_codex_enabled: bool = False
    preview_enabled: bool = False
    preview_backend: str = "docker"
    preview_controller_url: str = ""
    preview_controller_token: SecretStr = SecretStr("")
    preview_root: Path = Path("previews")
    preview_image: str = "koyorina-preview-runtime:local"
    preview_port_base: int = 8101
    preview_port_count: int = 4
    # Docker Compose版で、管理アプリ自身がコンテナの中にいるときのDocker網の名前。
    # 指定するとプレビューをその網へ繋ぎ、ホストへポートを出さずコンテナ名で届く。
    # 空なら従来どおり（管理アプリはホスト上、プレビューは127.0.0.1のポート）。
    preview_docker_network: str = ""
    # APP_ENV=local の開発者ログインを受け付ける接続元（カンマ区切りのIP）。既定は
    # ループバックのみ。Docker Compose版だけ、ホストからの接続が見えるDocker網の
    # ゲートウェイを足す。localでない環境では指定できない。
    local_client_hosts: str = ""

    @property
    def local_client_host_set(self) -> set[str]:
        return {item.strip() for item in self.local_client_hosts.split(",") if item.strip()}

    @property
    def apps_suffix(self) -> str:
        return app_hosts.host_suffix(self.app_origin, self.app_host_suffix)
    # 依存の取得元。生成アプリの npm / uv をここへ向ける。空なら公開レジストリ。
    preview_npm_registry: str = ""
    preview_npm_min_release_age: str = ""
    preview_pypi_index: str = ""
    vertex_project: str = "your-gcp-project-id"
    vertex_location: str = ""
    vertex_model: str = ""
    # 本体の Gemini（PDF読取・音声入力）の思考レベル。空ならモデルの既定。システム設定で上書きできる。
    gemini_thinking_level: str = ""
    # vertex=ADCを使うVertex AI / developer=Google AI StudioのAPIキーを使うGemini API。
    gemini_api_backend: Literal["vertex", "developer"] = "vertex"
    gemini_api_key: SecretStr = SecretStr("")
    # テナントに登録する秘密（生成アプリ用のGemini APIキー）を暗号化する鍵。32文字以上。
    # 空なら秘密の登録を受け付けない（Vertex AI のWIFは秘密が要らないので使える）。
    tenant_secret_key: SecretStr = SecretStr("")
    # 選択肢に出すGeminiのモデル。公開一覧APIは最新版を含まないため設定で持つ。
    gemini_models: str = "gemini-3.8-flash,gemini-3.5-flash"
    # AntigravityはGeminiのVertex/Developer選択とは独立したRemote Sandbox経路。
    antigravity_enabled: bool = False
    antigravity_model: str = "gemini-3.8-flash"
    openai_compatible_enabled: bool = False
    openai_compatible_model: str = ""
    openai_compatible_base_url: str = ""
    openai_compatible_label: str = "OpenAI互換API"
    openai_compatible_api_key: SecretStr = SecretStr("")
    # Claude（Claude Agent SDK）。画面（システム設定・テナント設定）で有効にする。
    claude_enabled: bool = False
    claude_model: str = "claude-sonnet-5"
    claude_backend: str = "api_key"
    # 生成アプリのイメージの置き場。private=クラスタ内のRegistry / artifact=Artifact Registry。
    # 本番はartifactを想定する。取り込んだイメージに脆弱性検査が付くため。
    app_registry_kind: str = "private"
    app_registry_host: str = "registry.koyorina-registry.svc:5000"

    publication_enabled: bool = False
    publication_controller_url: str = ""
    publication_controller_token: SecretStr = SecretStr("")
    app_registry_scanning_enabled: bool = False

    @model_validator(mode="after")
    def safe_configuration(self):
        # 最長のService名（-preview-controller）も63文字以内に収める。
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,42}[a-z0-9]|[a-z]", self.app_name):
            raise ValueError("APP_NAMEは小文字英字で始まる44文字以内のDNS名にしてください。")
        if self.app_registry_kind == 'unconfigured' and not self.app_registry_host:
            pass  # 保存先はシステム設定画面で選ぶ。
        else:
            app_images.validate(self.app_registry_kind, self.app_registry_host)
        if self.publication_enabled:
            expected = f"http://{self.app_name}-publish-controller.{self.app_name}-build.svc:8080"
            allowed = {expected}
            if self.app_env == 'local':
                allowed.add(COMPOSE_PUBLICATION_CONTROLLER)
            if self.publication_controller_url not in allowed:
                raise ValueError("公開controllerは専用の内部サービスに限定してください。")
            if len(self.publication_controller_token.get_secret_value()) < 32:
                raise ValueError("公開controllerのサービス間認証鍵が必要です。")
        if self.codex_controller_url:
            if self.codex_controller_url not in {f"http://{self.app_name}-codex-controller.{self.app_name}-codex.svc:8080",
                                                 "http://127.0.0.1:8091", COMPOSE_CODEX_CONTROLLER}:
                raise ValueError("Codex controllerは専用の内部サービスに限定してください。")
            # 手元のcontroller・Docker Compose版のcontrollerはローカル検証でだけ使う。
            if self.app_env != "local" and self.codex_controller_url in {"http://127.0.0.1:8091",
                                                                         COMPOSE_CODEX_CONTROLLER}:
                raise ValueError("本番では専用Codex controllerを利用してください。")
            if len(self.codex_controller_token.get_secret_value()) < 32:
                raise ValueError("Codex controllerのサービス間認証鍵が必要です。")
        if self.preview_backend not in {"docker", "controller"}:
            raise ValueError("プレビュー実行基盤は docker か controller を指定してください。")
        if self.preview_enabled:
            if self.preview_backend == "docker":
                # Dockerデーモン操作はホストのroot相当。共有環境ではコントローラ経由に限る。
                if self.app_env != "local":
                    raise ValueError("Docker直接操作のプレビューはローカル検証専用です。共有環境ではcontrollerを使ってください。")
                if not 1024 <= self.preview_port_base <= 65000 or not 1 <= self.preview_port_count <= 16:
                    raise ValueError("プレビュー用ポート枠の設定が不正です。")
                if self.preview_docker_network and not re.fullmatch(
                        r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}", self.preview_docker_network):
                    raise ValueError("PREVIEW_DOCKER_NETWORK の名前が不正です。")
            elif self.preview_controller_url not in {
                    f"http://{self.app_name}-preview-controller.{self.app_name}-preview.svc:8080",
                    "http://127.0.0.1:8092"}:
                raise ValueError("プレビューcontrollerは専用の内部サービスに限定してください。")
            elif len(self.preview_controller_token.get_secret_value()) < 32:
                raise ValueError("プレビューcontrollerのサービス間認証鍵が必要です。")
        elif self.preview_backend == "controller" and self.preview_controller_url:
            raise ValueError("プレビューを無効にしたままcontrollerを設定しないでください。")
        # APIキーは管理APIでは不要。実際の推論PodへだけSecretとして渡し、
        # ControllerSettings側で有効化時の存在を検証する。
        if self.openai_compatible_enabled and not (self.openai_compatible_base_url
                                                    and self.openai_compatible_model):
            raise ValueError("OpenAI互換APIを有効にするには接続先とモデルが必要です。")
        if not self.database_url.startswith("postgresql+psycopg://"):
            raise ValueError("PostgreSQL接続URLを設定してください。")
        if self.app_env not in {"local", "production"}:
            raise ValueError("APP_ENV は local または production にしてください。")
        if not re.fullmatch(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*",
                            self.apps_suffix):
            raise ValueError("APP_HOST_SUFFIX にはホスト名（例: apps.example.com）を指定してください。")
        if self.app_env == "local" and self.apps_suffix != "localhost":
            # *.localhost はブラウザがループバックへ向ける。それ以外は手元で名前解決できない。
            raise ValueError("ローカルモードの APP_HOST_SUFFIX は localhost にしてください。")
        if self.local_client_hosts:
            if self.app_env != "local":
                raise ValueError("LOCAL_CLIENT_HOSTS はローカル検証(APP_ENV=local)でだけ使えます。")
            for host in self.local_client_host_set:
                address = ipaddress.ip_address(host)  # 不正なら ValueError
                if not address.is_private:
                    raise ValueError("LOCAL_CLIENT_HOSTS にはプライベートアドレスだけを指定してください。")
        if self.app_env == "local":
            if any(os.getenv(k) for k in ("K_SERVICE", "KUBERNETES_SERVICE_HOST", "GAE_ENV")):
                raise ValueError("クラウド上で開発用認証を有効にできません。")
            origin = urlsplit(self.app_origin)
            if (origin.scheme != 'http' or origin.hostname not in {'localhost', '127.0.0.1'}
                    or not origin.port or origin.username or origin.password
                    or origin.path or origin.query or origin.fragment):
                raise ValueError("ローカルモードはループバックでのみ利用できます。")
        elif len(self.app_session_secret) < 32 or not self.google_oauth_client_id or not self.app_origin.startswith("https://"):
            raise ValueError("本番用Google認証、HTTPS Origin、32文字以上のセッション鍵が必要です。")
        return self
