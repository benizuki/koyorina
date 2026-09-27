import json
from datetime import datetime, timezone

from backend.domain.network_audit import summarize


NOW = datetime(2026, 9, 20, 13, 50, tzinfo=timezone.utc)


def flow(uuid, *, verdict="FORWARDED", destination_names=None, destination=None,
         destination_ip="34.102.162.58", port=443, reason="NEW", source_port=53000,
         flags=None):
    return json.dumps({"flow": {
        "uuid": uuid, "time": "2026-09-20T13:49:00Z", "verdict": verdict,
        "traffic_direction": "EGRESS", "is_reply": False, "trace_reason": reason,
        "IP": {"source": "10.0.2.10", "destination": destination_ip},
        "l4": {"TCP": {"source_port": source_port, "destination_port": port,
                         "flags": flags or {}}},
        "source": {"namespace": "koyorina-codex", "pod_name": "agent-user-tenant"},
        "destination": destination or {"labels": ["reserved:world"]},
        "destination_names": destination_names or [],
    }})


def test_summarizes_duplicate_node_flows_and_prefers_fqdn():
    same = flow("same", destination_names=["pypi.example.com", "npm.example.com"])
    result = summarize([same, same], now=NOW)
    assert result["observed"] == 1
    assert result["totals"]["allowed"] == 1
    row = result["rows"][0]
    assert row["source"] == "koyorina-codex/agent-user-tenant"
    assert row["destination"] == "npm.example.com / pypi.example.com"
    assert row["protocol"] == "HTTPS" and row["port"] == 443


def test_blocked_flows_are_first_and_internal_pods_use_names():
    internal = {"namespace": "kube-system", "pod_name": "coredns-abc"}
    result = summarize([
        flow("allowed", destination=internal, destination_ip="10.0.1.2", port=53),
        flow("blocked", verdict="DROPPED", destination_ip="169.254.169.254",
             reason="POLICY_DENIED"),
    ], now=NOW)
    assert result["rows"][0]["result"] == "blocked"
    assert result["rows"][0]["reason"] == "POLICY_DENIED"
    assert result["rows"][1]["destination"] == "kube-system/coredns-abc"


def test_kubernetes_api_gets_a_readable_name():
    destination = {"labels": ["reserved:host"]}
    result = summarize([flow("api", destination=destination, destination_ip="192.168.110.70",
                             port=6443)], now=NOW)
    assert result["rows"][0]["destination"] == "Kubernetes API"


def test_tcp_completion_is_counted_once_across_observation_points():
    first = flow("finish-1", flags={"FIN": True, "ACK": True})
    duplicate = flow("finish-2", flags={"FIN": True, "ACK": True})
    reset = flow("reset", source_port=53001, flags={"RST": True})
    result = summarize([first, duplicate, reset], now=NOW)
    assert result["observed"] == 2
    assert result["totals"]["allowed"] == 1
    assert result["totals"]["reset"] == 1


def test_old_ingress_reply_and_malformed_lines_are_ignored():
    old = json.loads(flow("old")); old["flow"]["time"] = "2026-09-20T12:00:00Z"
    reply = json.loads(flow("reply")); reply["flow"]["is_reply"] = True
    ingress = json.loads(flow("ingress")); ingress["flow"]["traffic_direction"] = "INGRESS"
    result = summarize([json.dumps(old), json.dumps(reply), json.dumps(ingress), "{"], now=NOW)
    assert result["observed"] == 0 and result["rows"] == []
