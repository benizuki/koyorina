"""Build the capability-free Codex Bubblewrap OCI seccomp profile (amd64 or arm64).

amd64 is what the k3s agent nodes use. The Docker Compose trial uses one profile for
both amd64 and arm64 (compose), so it starts on Apple Silicon without choosing a file:
a rule is kept when it applies to either architecture.

Input: moby/profiles 61eaf32614c7c71b60bd8927d3e6a4ffc8ff1f31.
Prints JSON only; does not install anything or change a running workload.
"""
import json
from pathlib import Path
import sys

ARCHITECTURES = {"amd64": "SCMP_ARCH_X86_64", "arm64": "SCMP_ARCH_AARCH64"}


def build_profile(arch="amd64"):
    arches = ("amd64", "arm64") if arch == "compose" else (arch,)
    source = json.loads(Path(__file__).with_name("moby-default.json").read_text())
    rules = []
    # Resolve Docker-specific conditionals for these non-root/drop-ALL pods on one architecture.
    for original in source["syscalls"]:
        include = original.get("includes", {})
        exclude = original.get("excludes", {})
        if include.get("caps") or (include.get("arches")
                                   and not set(arches) & set(include["arches"])):
            continue
        if set(arches) <= set(exclude.get("arches", [])):
            continue
        rule = {k: v for k, v in original.items() if k not in {"includes", "excludes", "comment"}}
        # The source-only worker does not need process inspection or alternate ABIs.
        rule["names"] = [n for n in rule["names"] if n not in {
            "ptrace", "process_vm_readv", "process_vm_writev", "modify_ldt", "clone"}]
        if rule["names"]:
            rules.append(rule)
    # Bubblewrap must create nested namespaces and build their filesystem view.
    # These calls remain gated by kernel user-namespace capabilities. No host
    # capability, host mount, host PID/network namespace, or SA token is granted.
    rules.append({"names": ["clone", "unshare", "setns", "mount", "umount2", "pivot_root", "chroot"],
                  "action": "SCMP_ACT_ALLOW"})
    return {"defaultAction": "SCMP_ACT_ERRNO", "defaultErrnoRet": 1,
            "architectures": [ARCHITECTURES[name] for name in arches], "syscalls": rules}


if __name__ == "__main__":
    print(json.dumps(build_profile(sys.argv[1] if len(sys.argv) > 1 else "amd64"), indent=2))
