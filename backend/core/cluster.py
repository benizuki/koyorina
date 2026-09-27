"""クラスタの状態を読むだけの窓口。操作はしない。

管理アプリに与えるのは、決まった名前空間のPod・Service・Deploymentを
一覧する権限だけ。作成も削除もできない。画面で状態を確かめるために使う。
"""
from datetime import datetime, timezone
from pathlib import Path
import asyncio
import re
import ssl
from urllib.parse import quote
import httpx
from backend.domain.network_audit import summarize

TOKEN_PATH = Path("/var/run/secrets/kubernetes.io/serviceaccount/token")
CA_PATH = Path("/var/run/secrets/kubernetes.io/serviceaccount/ca.crt")
NAMESPACES = ("koyorina", "koyorina-codex", "koyorina-preview")
AUDIT_NAMESPACE = "koyorina-observability"
POD_NAME = re.compile(r"[a-z0-9](?:[-a-z0-9.]*[a-z0-9])?")
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
AUTHORIZATION = re.compile(r"(?i)(authorization\s*[=:]\s*)(?:bearer\s+)?[^\s,;]+")
SENSITIVE = re.compile(
    r"(?i)(api[_-]?key|password|secret|token)(\s*[=:]\s*)([^\s,;]+)")


def clean_log_line(raw: str) -> str:
    """端末装飾と、よくある秘密値の書式を画面へ渡さない。"""
    clean = ANSI.sub("", raw).replace("\x00", "")
    clean = AUTHORIZATION.sub(r"\1***", clean)
    return SENSITIVE.sub(r"\1\2***", clean)


def available() -> bool:
    return TOKEN_PATH.is_file() and CA_PATH.is_file()


def age(timestamp: str | None) -> str:
    if not timestamp:
        return ""
    started = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    seconds = int((datetime.now(timezone.utc) - started).total_seconds())
    if seconds < 3600:
        return f"{seconds // 60}分"
    if seconds < 86400:
        return f"{seconds // 3600}時間"
    return f"{seconds // 86400}日"


def digest(image: str) -> str:
    """画面では末尾だけ見れば十分。どの版かの見分けがつけばよい。"""
    if "@sha256:" in image:
        name, sha = image.split("@sha256:", 1)
        return f"{name.rsplit('/', 1)[-1]}@{sha[:12]}"
    return image.rsplit("/", 1)[-1]


def pod_view(item: dict) -> dict:
    status = item.get("status", {})
    containers = status.get("containerStatuses") or []
    return {
        "name": item["metadata"]["name"],
        "phase": status.get("phase", "Unknown"),
        "ready": sum(1 for c in containers if c.get("ready")),
        "containers": len(containers) or len(item["spec"].get("containers", [])),
        "restarts": sum(int(c.get("restartCount", 0)) for c in containers),
        "node": item["spec"].get("nodeName", ""),
        "age": age(item["metadata"].get("creationTimestamp")),
        "images": [digest(c.get("image", "")) for c in item["spec"].get("containers", [])],
    }


def service_view(item: dict) -> dict:
    spec = item.get("spec", {})
    return {
        "name": item["metadata"]["name"],
        "type": spec.get("type", "ClusterIP"),
        "cluster_ip": spec.get("clusterIP", ""),
        "ports": [f"{port.get('port')}→{port.get('targetPort')}" for port in spec.get("ports", [])],
    }


def deployment_view(item: dict) -> dict:
    status = item.get("status", {})
    return {
        "name": item["metadata"]["name"],
        "ready": int(status.get("readyReplicas", 0)),
        "desired": int(item.get("spec", {}).get("replicas", 0)),
        "age": age(item["metadata"].get("creationTimestamp")),
    }


def app_namespaces(app_name: str) -> tuple[str, str, str]:
    """検証済みの運用設定から閲覧対象を決める。リクエストでは指定させない。"""
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,42}[a-z0-9]|[a-z]", app_name):
        raise ValueError("Invalid app name")
    return (app_name, f"{app_name}-codex", f"{app_name}-preview")


async def read(namespaces=NAMESPACES) -> dict:
    """名前空間ごとの一覧。取得失敗は空一覧にせずAPIのエラーとして伝える。"""
    token = TOKEN_PATH.read_text().strip()
    context = ssl.create_default_context(cafile=str(CA_PATH))
    result = []
    async with httpx.AsyncClient(verify=context, timeout=10, trust_env=False,
                                 headers={"Authorization": "Bearer " + token}) as client:
        for namespace in namespaces:
            section = {"namespace": namespace, "pods": [], "services": [], "deployments": []}
            for key, path, view in (
                    ("pods", f"/api/v1/namespaces/{namespace}/pods", pod_view),
                    ("services", f"/api/v1/namespaces/{namespace}/services", service_view),
                    ("deployments", f"/apis/apps/v1/namespaces/{namespace}/deployments", deployment_view)):
                response = await client.get("https://kubernetes.default.svc" + path)
                response.raise_for_status()
                section[key] = [view(item) for item in response.json().get("items", [])][:50]
            result.append(section)
    return {"namespaces": result}


async def read_logs(namespace: str, pod: str, tail_lines: int = 400, *, namespaces=NAMESPACES) -> dict:
    """許可済み名前空間のPodから、画面表示に必要な直近ログだけを読む。"""
    if namespace not in namespaces or not POD_NAME.fullmatch(pod):
        raise ValueError("対象のPodを選び直してください。")
    tail_lines = min(max(tail_lines, 50), 1000)
    token = TOKEN_PATH.read_text().strip()
    context = ssl.create_default_context(cafile=str(CA_PATH))
    path = (f"/api/v1/namespaces/{quote(namespace, safe='')}/pods/"
            f"{quote(pod, safe='')}/log?tailLines={tail_lines}&timestamps=true&limitBytes=262144")
    async with httpx.AsyncClient(verify=context, timeout=15, trust_env=False,
                                 headers={"Authorization": "Bearer " + token}) as client:
        response = await client.get("https://kubernetes.default.svc" + path)
    if response.status_code == 404:
        raise LookupError("Podが終了したためログを取得できませんでした。")
    response.raise_for_status()
    lines = []
    for raw in response.text.splitlines():
        lines.append(clean_log_line(raw))
    return {"namespace": namespace, "pod": pod, "lines": lines[-tail_lines:]}


async def read_network_flows(minutes: int = 15, verdict: str = "all") -> dict:
    """各ノードの監査readerから、管理画面向けの接続開始・拒否だけを読む。"""
    token = TOKEN_PATH.read_text().strip()
    context = ssl.create_default_context(cafile=str(CA_PATH))
    headers = {"Authorization": "Bearer " + token}
    base = "https://kubernetes.default.svc"
    pod_path = (f"/api/v1/namespaces/{AUDIT_NAMESPACE}/pods"
                "?labelSelector=app%3Daudit-collector")
    lines = []
    async with httpx.AsyncClient(verify=context, timeout=20, trust_env=False,
                                 headers=headers) as client:
        response = await client.get(base + pod_path)
        if response.status_code == 404:
            return {"available": False, "window_minutes": minutes, "observed": 0,
                    "totals": {"allowed": 0, "blocked": 0, "reset": 0, "unknown": 0},
                    "rows": [], "truncated": False}
        response.raise_for_status()
        pods = [item["metadata"]["name"] for item in response.json().get("items", [])
                if item.get("status", {}).get("phase") == "Running"]
        async def fetch(pod: str):
            # Pod proxyはこの環境では名前付きportを解決せず、応答前に切断する。
            # RBACとCiliumでreaderの8081だけを許可しているため、数値で固定する。
            target = quote(f"{pod}:8081", safe=":")
            path = f"/api/v1/namespaces/{AUDIT_NAMESPACE}/pods/{target}/proxy/cgi-bin/flows"
            # Pod proxyのCGI応答にはContent-Lengthがない。Pod一覧のkeep-alive接続を
            # 再利用するとapiserverが本文前に切断するため、readerごとに接続を閉じる。
            async with httpx.AsyncClient(verify=context, timeout=30, trust_env=False,
                                         headers={**headers, "Connection": "close"}) as reader:
                return await reader.get(base + path)

        successful = 0
        for result in await asyncio.gather(*(fetch(pod) for pod in pods),
                                           return_exceptions=True):
            if isinstance(result, Exception):
                continue
            if result.is_success:
                successful += 1
                lines.extend(result.text.splitlines())
    if not pods:
        return {"available": False, "window_minutes": minutes, "observed": 0,
                "totals": {"allowed": 0, "blocked": 0, "reset": 0, "unknown": 0},
                "rows": [], "truncated": False}
    if not successful:
        return {"available": False, "window_minutes": minutes, "observed": 0,
                "totals": {"allowed": 0, "blocked": 0, "reset": 0, "unknown": 0},
                "rows": [], "truncated": False,
                "readers": {"available": 0, "total": len(pods)}}
    result = summarize(lines, minutes=minutes, verdict=verdict)
    result["readers"] = {"available": successful, "total": len(pods)}
    return result
