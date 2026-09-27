"""Koyorina 本体が、自分の ServiceAccount のトークンを audience 付きで取得する。

Workload Identity 連携の audience は画面（システム設定）で変えられる。投影ボリュームだと
audience が Pod の定義に固定されるので、TokenRequest API で都度取得する。
必要な権限は、自分の ServiceAccount に対する serviceaccounts/token の create だけ
（setup/manifest/app.yaml の Role で名前を限っている）。
"""
from __future__ import annotations

import base64
import json
import ssl
import threading
import time
from pathlib import Path

import httpx
from google.auth import identity_pool

SERVICE_ACCOUNT_DIR = Path("/var/run/secrets/kubernetes.io/serviceaccount")
API = "https://kubernetes.default.svc"
LIFETIME = 3600
# 期限の少し前に取り直す。STSとの往復の途中で切れないように。
MARGIN = 300


class TokenUnavailable(RuntimeError):
    pass


def identity(directory: Path = SERVICE_ACCOUNT_DIR) -> tuple[str, str]:
    """自分の namespace と ServiceAccount 名。マウントされたトークンの sub から読む。"""
    try:
        token = (directory / "token").read_text().strip()
        payload = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        _, _, namespace, name = claims["sub"].split(":", 3)
        return namespace, name
    except (OSError, IndexError, KeyError, ValueError):
        raise TokenUnavailable("Kubernetes の ServiceAccount を確認できません。") from None


def subject(directory: Path = SERVICE_ACCOUNT_DIR) -> str:
    namespace, name = identity(directory)
    return f"system:serviceaccount:{namespace}:{name}"


class KubernetesTokenSupplier(identity_pool.SubjectTokenSupplier):
    """google-auth から呼ばれ、audience 付きのトークンを返す。期限まで使い回す。"""

    def __init__(self, audience: str, directory: Path = SERVICE_ACCOUNT_DIR):
        self.audience, self.directory = audience, directory
        self._token, self._expires, self._lock = "", 0.0, threading.Lock()

    def get_subject_token(self, context, request):
        with self._lock:
            if self._token and time.time() < self._expires - MARGIN:
                return self._token
            namespace, name = identity(self.directory)
            own = (self.directory / "token").read_text().strip()
            tls = ssl.create_default_context(cafile=str(self.directory / "ca.crt"))
            try:
                response = httpx.post(
                    f"{API}/api/v1/namespaces/{namespace}/serviceaccounts/{name}/token",
                    headers={"Authorization": "Bearer " + own}, verify=tls, timeout=15, trust_env=False,
                    json={"apiVersion": "authentication.k8s.io/v1", "kind": "TokenRequest",
                          "spec": {"audiences": [self.audience], "expirationSeconds": LIFETIME}})
                response.raise_for_status()
                self._token = response.json()["status"]["token"]
            except (httpx.HTTPError, KeyError, ValueError):
                raise TokenUnavailable("Kubernetes からトークンを取得できませんでした。"
                                       "serviceaccounts/token の権限を確認してください。") from None
            self._expires = time.time() + LIFETIME
            return self._token


def credentials(config: dict, supplier: KubernetesTokenSupplier):
    """external_account の設定のうち、トークンの取り方だけをこのプロセスに差し替える。"""
    return identity_pool.Credentials(
        audience=config["audience"], subject_token_type=config["subject_token_type"],
        token_url=config["token_url"], subject_token_supplier=supplier,
        service_account_impersonation_url=config.get("service_account_impersonation_url"),
        scopes=["https://www.googleapis.com/auth/cloud-platform"])


def cluster_get(path: str, directory: Path = SERVICE_ACCOUNT_DIR) -> dict:
    """OIDCの発見用の読み取り（発行元と公開鍵）。秘密ではない。"""
    own = (directory / "token").read_text().strip()
    tls = ssl.create_default_context(cafile=str(directory / "ca.crt"))
    try:
        response = httpx.get(API + path, headers={"Authorization": "Bearer " + own},
                             verify=tls, timeout=15, trust_env=False)
        response.raise_for_status()
        return response.json()
    except (httpx.HTTPError, ValueError):
        raise TokenUnavailable("クラスタのOIDC情報を読めませんでした。") from None
