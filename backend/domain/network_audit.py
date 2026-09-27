"""Hubbleの生フローを、管理画面で読める通信単位へまとめる。"""
from datetime import datetime, timedelta, timezone
import ipaddress
import json
from urllib.parse import urlsplit


def _time(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None


def _pod(endpoint: dict) -> str:
    pod = endpoint.get("pod_name")
    namespace = endpoint.get("namespace")
    if pod:
        return f"{namespace}/{pod}" if namespace else pod
    workloads = endpoint.get("workloads") or []
    if workloads:
        name = workloads[0].get("name")
        return f"{namespace}/{name}" if namespace and name else (name or "名前不明")
    return "名前不明"


def _fqdn(flow: dict) -> list[str]:
    names = {str(value).rstrip(".") for value in flow.get("destination_names") or [] if value}
    for label in (flow.get("destination") or {}).get("labels") or []:
        if label.startswith("fqdn:"):
            names.add(label.removeprefix("fqdn:").rstrip("."))
    http = ((flow.get("l7") or {}).get("http") or {})
    url = http.get("url") or ""
    if url:
        host = urlsplit(url if "://" in url else "//" + url).hostname
        if host:
            names.add(host.rstrip("."))
    return sorted(names)


def _destination(flow: dict) -> tuple[str, str | None]:
    names = _fqdn(flow)
    ip = (flow.get("IP") or {}).get("destination")
    if names:
        return " / ".join(names), ip
    endpoint = flow.get("destination") or {}
    if endpoint.get("pod_name") or endpoint.get("workloads"):
        return _pod(endpoint), ip
    l4 = flow.get("l4") or {}
    tcp = l4.get("TCP") or {}
    if tcp.get("destination_port") == 6443 and "reserved:host" in (endpoint.get("labels") or []):
        return "Kubernetes API", ip
    try:
        address = ipaddress.ip_address(ip)
        scope = "クラスタ内の接続先（名前不明）" if address.is_private else "外部接続先（名前不明）"
    except (TypeError, ValueError):
        scope = "接続先（名前不明）"
    return scope, ip


def _protocol(flow: dict) -> tuple[str, int | None]:
    l7 = flow.get("l7") or {}
    if l7.get("http"):
        method = l7["http"].get("method")
        return (f"HTTP {method}" if method else "HTTP"), None
    if l7.get("dns"):
        return "DNS", 53
    l4 = flow.get("l4") or {}
    if not l4:
        return flow.get("Type") or "不明", None
    name, detail = next(iter(l4.items()))
    port = detail.get("destination_port") if isinstance(detail, dict) else None
    if name.upper() == "TCP" and port == 443:
        return "HTTPS", port
    if name.upper() in {"TCP", "UDP"} and port == 53:
        return "DNS", port
    if name.upper() == "TCP" and port == 80:
        return "HTTP", port
    return name.upper(), port


def _result(verdict: str, reset: bool = False) -> str:
    if verdict == "FORWARDED":
        return "reset" if reset else "allowed"
    if verdict in {"DROPPED", "ERROR"}:
        return "blocked"
    return "unknown"


def summarize(lines: list[str], minutes: int = 15, verdict: str = "all",
              now: datetime | None = None, limit: int = 200) -> dict:
    """NDJSONを接続先単位に集約する。壊れた行は画面全体を止めず読み飛ばす。"""
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(minutes=minutes)
    grouped: dict[tuple, dict] = {}
    seen: set[str] = set()
    seen_sessions: set[tuple] = set()
    observed = 0
    for line in lines:
        try:
            flow = json.loads(line).get("flow") or {}
        except (json.JSONDecodeError, AttributeError):
            continue
        uuid = str(flow.get("uuid") or "")
        if uuid and uuid in seen:
            continue
        if uuid:
            seen.add(uuid)
        timestamp = _time(flow.get("time"))
        if not timestamp or timestamp < since or flow.get("traffic_direction") != "EGRESS":
            continue
        if flow.get("is_reply") is True:
            continue
        tcp = (flow.get("l4") or {}).get("TCP") or {}
        flags = tcp.get("flags") or {}
        if flags.get("FIN") or flags.get("RST"):
            network = flow.get("IP") or {}
            session = (network.get("source"), network.get("destination"),
                       tcp.get("source_port"), tcp.get("destination_port"),
                       bool(flags.get("FIN")), bool(flags.get("RST")))
            if session in seen_sessions:
                continue
            seen_sessions.add(session)
        result = _result(str(flow.get("verdict") or ""), bool(flags.get("RST")))
        if verdict != "all" and result != verdict:
            continue
        source = _pod(flow.get("source") or {})
        destination, destination_ip = _destination(flow)
        protocol, port = _protocol(flow)
        reason = str(flow.get("drop_reason_desc") or
                     ("TCPリセット" if flags.get("RST") else "TCP完了" if flags.get("FIN") else "") or
                     flow.get("trace_reason") or "")
        key = (source, destination, protocol, port, result)
        observed += 1
        row = grouped.setdefault(key, {
            "source": source,
            "destination": destination,
            "destination_ip": destination_ip,
            "protocol": protocol,
            "port": port,
            "result": result,
            "reason": reason,
            "count": 0,
            "last_seen": flow.get("time"),
        })
        row["count"] += 1
        if result == "blocked" and reason:
            row["reason"] = reason
        if str(flow.get("time") or "") > str(row["last_seen"] or ""):
            row["last_seen"] = flow.get("time")
    rows = sorted(grouped.values(), key=lambda row: (
        row["result"] != "blocked", str(row["last_seen"])), reverse=False)
    # blockedを先に、その中では新しい順にする。
    rows = sorted(rows, key=lambda row: row["last_seen"] or "", reverse=True)
    rows = sorted(rows, key=lambda row: row["result"] != "blocked")
    totals = {"allowed": 0, "blocked": 0, "reset": 0, "unknown": 0}
    for row in rows:
        totals[row["result"]] += row["count"]
    return {
        "available": True,
        "window_minutes": minutes,
        "observed": observed,
        "totals": totals,
        "rows": rows[:limit],
        "truncated": len(rows) > limit,
    }
