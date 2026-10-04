"""クラスタの状態を読むだけの窓口。操作はしない。

管理アプリに与えるのは、決まった名前空間のPod・Service・Deploymentを
一覧する権限だけ。作成も削除もできない。画面で状態を確かめるために使う。
"""
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID
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


def pod_problem(item: dict) -> tuple[str, str]:
    """起動待ち・終了・スケジュール失敗の理由を秘密値を除いて返す。"""
    status = item.get("status", {})
    for container in (status.get("initContainerStatuses") or []) + (status.get("containerStatuses") or []):
        state = container.get("state", {})
        problem = state.get("waiting") or state.get("terminated") or {}
        if problem and problem.get("reason") != "Completed":
            return problem.get("reason", "Unknown"), clean_log_line(problem.get("message", ""))[:1000]
    for condition in status.get("conditions", []):
        if condition.get("type") == "PodScheduled" and condition.get("status") == "False":
            return condition.get("reason", "Unschedulable"), clean_log_line(condition.get("message", ""))[:1000]
    return "", ""


def pod_view(item: dict) -> dict:
    status = item.get("status", {})
    labels = item.get("metadata", {}).get("labels") or {}
    project_id = labels.get("koyorina/project", "")
    preview_name = labels.get("app.kubernetes.io/name", "")
    published_name = labels.get("koyorina-published", "")
    if not project_id and preview_name.startswith("preview-"):
        project_id = preview_name.removeprefix("preview-")
    if not project_id and published_name.startswith("published-"):
        project_id = published_name.removeprefix("published-")
    try:
        project_id = str(UUID(project_id)) if project_id else ""
    except ValueError:
        project_id = ""
    containers = status.get("containerStatuses") or []
    reason, message = pod_problem(item)
    return {
        "reason": reason, "message": message,
        "project_id": project_id,
        "build_id": labels.get("koyorina-build", ""),
        "user_id": labels.get("forge-user", ""),
        "tenant_id": labels.get("forge-tenant", ""),
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


def app_namespaces(app_name: str, publication_enabled: bool = False) -> tuple[str, ...]:
    """検証済みの運用設定から閲覧対象を決める。リクエストでは指定させない。"""
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,42}[a-z0-9]|[a-z]", app_name):
        raise ValueError("Invalid app name")
    namespaces = (app_name, f"{app_name}-codex", f"{app_name}-preview")
    return namespaces + ((f"{app_name}-build", f"{app_name}-published") if publication_enabled else ())


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


async def read_pod_details(namespace: str, pod: str, *, namespaces=NAMESPACES) -> dict:
    """Pod conditions and recent events explain scheduling, mount and pull waits."""
    if namespace not in namespaces or not POD_NAME.fullmatch(pod):
        raise ValueError("対象のPodを選び直してください。")
    token = TOKEN_PATH.read_text().strip()
    context = ssl.create_default_context(cafile=str(CA_PATH))
    base = f"https://kubernetes.default.svc/api/v1/namespaces/{quote(namespace, safe='')}"
    selector = quote(f"involvedObject.kind=Pod,involvedObject.name={pod}", safe="")
    async with httpx.AsyncClient(verify=context, timeout=10, trust_env=False,
                                 headers={"Authorization": "Bearer " + token}) as client:
        response = await client.get(f"{base}/pods/{quote(pod, safe='')}")
        if response.status_code == 404:
            raise LookupError("Podが終了しました。状態を更新してください。")
        response.raise_for_status()
        events = await client.get(f"{base}/events?fieldSelector={selector}")
        events.raise_for_status()
    item = response.json()
    conditions = [{"type": c.get("type", ""), "status": c.get("status", ""),
                   "reason": c.get("reason", ""),
                   "message": clean_log_line(c.get("message", ""))[:1000]}
                  for c in item.get("status", {}).get("conditions", [])]
    recent = sorted(events.json().get("items", []), key=lambda e:
                    e.get("lastTimestamp") or e.get("eventTime") or e.get("metadata", {}).get("creationTimestamp") or "",
                    reverse=True)[:30]
    return {"namespace": namespace, "pod": pod, "status": pod_view(item), "conditions": conditions,
            "events": [{"type": e.get("type", ""), "reason": e.get("reason", ""),
                        "message": clean_log_line(e.get("message", ""))[:1000],
                        "count": e.get("count", 1),
                        "at": e.get("lastTimestamp") or e.get("eventTime") or e.get("metadata", {}).get("creationTimestamp") or ""}
                       for e in recent]}


async def read_published(namespace: str, name: str) -> dict:
    """Read one published workload without truncating a shared namespace listing."""
    if not POD_NAME.fullmatch(namespace) or not POD_NAME.fullmatch(name) or not name.startswith('published-'):
        raise ValueError('対象の公開アプリを選び直してください。')
    token = TOKEN_PATH.read_text().strip()
    context = ssl.create_default_context(cafile=str(CA_PATH))
    base = f'https://kubernetes.default.svc/api/v1/namespaces/{quote(namespace, safe="")}'
    selector = quote(f'koyorina-published={name}', safe='')
    async with httpx.AsyncClient(verify=context, timeout=10, trust_env=False,
                                 headers={'Authorization': 'Bearer ' + token}) as client:
        pods = await client.get(f'{base}/pods?labelSelector={selector}')
        pods.raise_for_status()
        deployment = await client.get(f'https://kubernetes.default.svc/apis/apps/v1/namespaces/'
            f'{quote(namespace, safe="")}/deployments/{quote(name, safe="")}')
        if deployment.status_code != 404:
            deployment.raise_for_status()
    return {'pods': [pod_view(item) for item in pods.json().get('items', [])],
            'deployment': deployment_view(deployment.json()) if deployment.status_code != 404 else None}


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
