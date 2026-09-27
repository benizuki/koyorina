"""Deterministic Kubernetes names for tenant-isolated generation storage."""
from hashlib import sha256
from uuid import UUID


_BINARY_QUANTITIES = {"Ki": 1024, "Mi": 1024 ** 2, "Gi": 1024 ** 3,
                      "Ti": 1024 ** 4}


def canonical_tenant_id(value: str | UUID) -> str:
    """Accept only UUID tenant identifiers before they reach Kubernetes."""
    return str(UUID(str(value)))


def tenant_hash(value: str | UUID, length: int = 12) -> str:
    tenant = canonical_tenant_id(value)
    return sha256(tenant.encode("ascii")).hexdigest()[:length]


def generation_claim(value: str | UUID) -> str:
    return f"koyorina-generation-{tenant_hash(value, 16)}"


def preview_claim(value: str | UUID) -> str:
    return f"koyorina-preview-{tenant_hash(value, 16)}"


def generation_worker(user_id: str | UUID, tenant_id: str | UUID) -> str:
    """One generation worker per user and tenant, shared by Codex and Gemini."""
    user = UUID(str(user_id)).hex[:12]
    return f"agent-{user}-{tenant_hash(tenant_id, 12)}"


def legacy_generation_worker(provider: str, user_id: str | UUID,
                             tenant_id: str | UUID) -> str:
    """Names used before providers shared one generation worker.

    Kept only so the controller can remove an idle legacy worker before mounting the
    same RWO tenant and credential claims in the shared worker.
    """
    if provider not in {"codex", "gemini"}:
        raise ValueError("unsupported provider")
    user = UUID(str(user_id)).hex[:12]
    return f"{provider}-{user}-{tenant_hash(tenant_id, 12)}"


def auth_worker(user_id: str | UUID) -> str:
    return f"codex-auth-{UUID(str(user_id)).hex[:20]}"


def quantity_bytes(value: str) -> int:
    """Parse the fixed Kubernetes storage quantities accepted by this application."""
    text = str(value).strip()
    for suffix, factor in _BINARY_QUANTITIES.items():
        if text.endswith(suffix):
            return int(text[:-len(suffix)]) * factor
    return int(text)


def storage_measurement_pod(name: str, namespace: str, claim: str, image: str,
                            node_selector: dict[str, str], *, image_pull_secret: str = "",
                            toleration: bool = False, app_name: str = "koyorina") -> dict:
    """Build a read-only, one-PVC pod that reports logical usage and node filesystem space."""
    script = (
        "import json,os\n"
        "used=0; seen=set(); stack=['/volume']\n"
        "while stack:\n"
        " path=stack.pop()\n"
        " try:\n"
        "  with os.scandir(path) as entries:\n"
        "   for entry in entries:\n"
        "    try:\n"
        "     if entry.is_symlink(): continue\n"
        "     if entry.is_dir(follow_symlinks=False): stack.append(entry.path); continue\n"
        "     stat=entry.stat(follow_symlinks=False); key=(stat.st_dev,stat.st_ino)\n"
        "     if key not in seen: seen.add(key); used+=stat.st_size\n"
        "    except OSError: pass\n"
        " except OSError: pass\n"
        "fs=os.statvfs('/volume')\n"
        "print(json.dumps({'used_bytes':used,'capacity_bytes':fs.f_blocks*fs.f_frsize,'available_bytes':fs.f_bavail*fs.f_frsize}))\n")
    spec = {"restartPolicy": "Never", "automountServiceAccountToken": False,
            **({"nodeSelector": node_selector} if node_selector else {}),
            "securityContext": {"runAsNonRoot": True, "runAsUser": 10001,
                                "runAsGroup": 10001, "fsGroupChangePolicy": "OnRootMismatch",
                                "seccompProfile": {"type": "RuntimeDefault"}},
            "containers": [{"name": "measure", "image": image,
                "command": ["python", "-c", script],
                "securityContext": {"allowPrivilegeEscalation": False,
                                    "readOnlyRootFilesystem": True,
                                    "capabilities": {"drop": ["ALL"]}},
                "resources": {"requests": {"cpu": "10m", "memory": "32Mi"},
                              "limits": {"cpu": "250m", "memory": "128Mi"}},
                "volumeMounts": [{"name": "storage", "mountPath": "/volume", "readOnly": True},
                                 {"name": "tmp", "mountPath": "/tmp"}]}],
            "volumes": [{"name": "storage", "persistentVolumeClaim": {"claimName": claim,
                                                                          "readOnly": True}},
                        {"name": "tmp", "emptyDir": {"sizeLimit": "16Mi"}}]}
    if image_pull_secret:
        spec["imagePullSecrets"] = [{"name": image_pull_secret}]
    if toleration:
        # 実際のtaintは workload=<APP_NAME>-agent:NoSchedule。値を
        # 合わせないと、taint済みのagentノードで計測Podが永久にPendingのまま終わらない。
        spec["tolerations"] = [{"key": "workload", "operator": "Equal",
                                "value": f"{app_name}-agent", "effect": "NoSchedule"}]
    return {"apiVersion": "v1", "kind": "Pod",
            "metadata": {"name": name, "namespace": namespace,
                         "labels": {"app": "koyorina-storage-measurement"}},
            "spec": spec}
