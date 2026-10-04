"""Fail the deployment before switching published apps if RWOP CSI cannot mount."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import uuid


def kubectl(*args: str, input_text: str | None = None) -> str:
    result = subprocess.run(
        ["kubectl", *args], input=input_text, text=True, capture_output=True, check=False
    )
    if result.returncode:
        raise RuntimeError(f"kubectl {' '.join(args)}: {result.stderr.strip()}")
    return result.stdout


def failure_details(namespace: str, reason: str) -> RuntimeError:
    details = []
    for resource in ("pod/storage-check", "pvc/data"):
        try:
            description = kubectl("-n", namespace, "describe", resource)
            details.append(f"{resource}:\n" + "\n".join(description.splitlines()[-45:]))
        except RuntimeError:
            pass
    return RuntimeError(reason + ("\n" + "\n".join(details) if details else ""))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--storage-class", required=True)
    parser.add_argument("--driver", required=True)
    parser.add_argument("--size", required=True)
    parser.add_argument("--image", default="python:3.14-slim")
    parser.add_argument("--relocate", action="store_true", help="Remount the same PVC on another ready node")
    args = parser.parse_args()
    namespace = f"koyorina-storage-check-{uuid.uuid4().hex[:10]}"
    sc = json.loads(kubectl("get", "storageclass", args.storage_class, "-o", "json"))
    if sc["provisioner"] != args.driver or sc.get("volumeBindingMode") != "WaitForFirstConsumer":
        raise RuntimeError("公開データ用StorageClassのCSIドライバーまたはbinding modeが異なります")
    claim = {
        "apiVersion": "v1", "kind": "PersistentVolumeClaim",
        "metadata": {"name": "data", "namespace": namespace},
        "spec": {"storageClassName": args.storage_class, "accessModes": ["ReadWriteOncePod"],
                 "resources": {"requests": {"storage": args.size}}},
    }
    pod = {
        "apiVersion": "v1", "kind": "Pod",
        "metadata": {"name": "storage-check", "namespace": namespace},
        "spec": {
            "restartPolicy": "Never",
            "containers": [{"name": "check", "image": args.image,
                            "command": ["sh", "-c", "echo rwop-ok > /data/check && sleep 1800"],
                            "volumeMounts": [{"name": "data", "mountPath": "/data"}]}],
            "volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": "data"}}],
        },
    }
    kubectl("create", "namespace", namespace)
    try:
        kubectl("apply", "-f", "-", input_text=json.dumps(claim))
        kubectl("apply", "-f", "-", input_text=json.dumps(pod))
        try:
            kubectl("-n", namespace, "wait", "--for=condition=Ready", "pod/storage-check", "--timeout=5m")
        except RuntimeError as exc:
            raise failure_details(namespace, str(exc)) from exc
        pvc = json.loads(kubectl("-n", namespace, "get", "pvc", "data", "-o", "json"))
        pv = json.loads(kubectl("get", "pv", pvc["spec"]["volumeName"], "-o", "json"))
        if pvc["spec"]["accessModes"] != ["ReadWriteOncePod"] or pv["spec"]["csi"]["driver"] != args.driver:
            raise RuntimeError("作成したPVCのアクセスモードまたはPVのCSIドライバーが異なります")
        if kubectl("-n", namespace, "exec", "storage-check", "--", "cat", "/data/check").strip() != "rwop-ok":
            raise RuntimeError("PVCへの書き込み・読み出しに失敗しました")
        if args.relocate:
            current = json.loads(kubectl("-n", namespace, "get", "pod", "storage-check", "-o", "json"))["spec"]["nodeName"]
            nodes = json.loads(kubectl("get", "nodes", "-o", "json"))["items"]
            by_name = {node["metadata"]["name"]: node for node in nodes}
            zone = by_name[current]["metadata"].get("labels", {}).get("topology.kubernetes.io/zone")
            alternatives = [node["metadata"]["name"] for node in nodes
                if node["metadata"]["name"] != current
                and (not zone or node["metadata"].get("labels", {}).get("topology.kubernetes.io/zone") == zone)
                and any(condition.get("type") == "Ready" and condition.get("status") == "True"
                        for condition in node.get("status", {}).get("conditions", []))]
            if not alternatives:
                raise RuntimeError("同じゾーンの別のReadyノードがありません")
            kubectl("-n", namespace, "delete", "pod", "storage-check", "--wait=true", "--timeout=5m")
            pod["metadata"]["name"] = "storage-check-remount"
            pod["spec"]["nodeName"] = alternatives[0]
            pod["spec"]["containers"][0]["command"] = [
                "sh", "-c", "test \"$(cat /data/check)\" = rwop-ok && sleep 1800"]
            kubectl("apply", "-f", "-", input_text=json.dumps(pod))
            kubectl("-n", namespace, "wait", "--for=condition=Ready", "pod/storage-check-remount", "--timeout=5m")
            if kubectl("-n", namespace, "exec", "storage-check-remount", "--", "cat", "/data/check").strip() != "rwop-ok":
                raise RuntimeError("別ノードでPVCのデータを読めません")
    finally:
        kubectl("delete", "namespace", namespace, "--wait=true", "--timeout=5m")
    print(f"RWOP CSI mount verified: {args.storage_class} ({args.driver})")


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, KeyError, ValueError) as exc:
        sys.exit(str(exc))
