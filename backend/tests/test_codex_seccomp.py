"""Static guardrails only: these do not prove live kernel compatibility."""
import json
from pathlib import Path
import runpy

import pytest


PROFILE_DIR = Path(__file__).resolve().parents[2] / "setup/manifest/seccomp"
# amd64はk3sのagentノード、arm64はDocker Compose版（Apple Silicon等）で使う。
# compose は Docker Compose版の両アーキ共用（どちらかで要る規則を残す）。
ARCHES = pytest.mark.parametrize("arch,scmp", [
    ("amd64", ["SCMP_ARCH_X86_64"]), ("arm64", ["SCMP_ARCH_AARCH64"]),
    ("compose", ["SCMP_ARCH_X86_64", "SCMP_ARCH_AARCH64"])])


@ARCHES
def test_seccomp_profile_is_reproducible_and_default_deny(arch, scmp):
    builder = runpy.run_path(str(PROFILE_DIR / "build_profile.py"))
    profile = json.loads((PROFILE_DIR / f"codex-bwrap-{arch}-v1.json").read_text())
    assert builder["build_profile"](arch) == profile
    assert profile["defaultAction"] == "SCMP_ACT_ERRNO"
    assert profile["architectures"] == scmp
    allowed = {n for rule in profile["syscalls"] if rule["action"] == "SCMP_ACT_ALLOW" for n in rule["names"]}
    assert {"unshare", "clone", "mount", "umount2", "pivot_root", "chroot", "setns"} <= allowed
    assert not {"bpf", "ptrace", "process_vm_readv", "process_vm_writev", "pidfd_getfd",
                "init_module", "finit_module", "delete_module", "reboot", "perf_event_open",
                "open_by_handle_at", "io_uring_setup"} & allowed
    assert all(not ({"includes", "excludes"} & rule.keys()) for rule in profile["syscalls"])
    assert next(r for r in profile["syscalls"] if "clone3" in r["names"])["errnoRet"] == 38


@ARCHES
def test_seccomp_profile_does_not_enable_kernel_crypto_or_host_vsock(arch, scmp):
    profile = json.loads((PROFILE_DIR / f"codex-bwrap-{arch}-v1.json").read_text())
    rules = [r for r in profile["syscalls"] if "socket" in r["names"]]
    for family in (38, 40):
        for rule in rules:
            arg = rule["args"][0]
            assert not {"SCMP_CMP_EQ": family == arg["value"],
                        "SCMP_CMP_LT": family < arg["value"],
                        "SCMP_CMP_GT": family > arg["value"]}[arg["op"]]
