# Codex Bubblewrap profile — installed on k3s-agent-2

The owner approved a Codex-only seccomp profile and a temporary, nonprivileged
installer Pod on 2026-09-11. Device-code authentication stays unchanged.
Native amd64 k3s protocol and permission-profile probes passed before the final
worker rollout. These probes do not validate a generated application's behavior.

`moby-default.json` is vendored from moby/profiles commit
`61eaf32614c7c71b60bd8927d3e6a4ffc8ff1f31`; its Apache-2.0 license is in LICENSE.
`build_profile.py` resolves Docker-specific conditionals for amd64/drop-ALL
containers into a standard OCI seccomp JSON document. The checked-in result is
`codex-bwrap-amd64-v1.json`. Tests assert exact reproducibility.

Compared with that capability-free baseline, this candidate allows clone,
unshare, setns, mount, umount2, pivot_root, and chroot so Bubblewrap can create
nested user/mount/network namespaces. This expands kernel attack surface: seccomp
cannot determine whether a call originates inside a nested user namespace.
Kernel capability checks, noNewPrivileges, drop-ALL, non-root, no host namespaces,
no service-account token, and the existing network policies must remain in place.
Do not treat this as equivalent to RuntimeDefault or as a kernel exploit boundary.
Process inspection calls are additionally removed; clone3 retains ENOSYS fallback.

## Probes

Two credential-free scripts verify the live sandbox. Neither is collected by pytest:
they need a real `codex` binary and must run **natively on an amd64 node**, inside the
pinned agent image. Mock unit tests are not proof that the sandbox actually starts.

```sh
python -m backend.tests.codex_protocol_smoke    # thread/start + Dynamic Tool + turn/start accepted
python -m backend.tests.codex_workspace_smoke   # filesystem and network confinement
```

- `codex_protocol_smoke.py` starts the app-server offline with the real permission
  profile and checks the protocol handshake plus registration of the no-argument
  `app_forge_install_dependencies` Dynamic Tool. It deliberately fails while the profile
  cannot start, so a failure here means the sandbox is not usable yet.
- `codex_workspace_smoke.py` asserts that a write inside the job workspace persists,
  that reads and persisted writes outside it are denied, and that a direct network
  connection is refused. It then checks the dependency-install variant of the same
  profile: network **is** allowed, but another application's workspace is still
  unreadable. Both halves matter — without the first, installs always fail; without
  the second, an install reaches every project on the shared volume. It finally checks
  that `.agents/skills/` inside the workspace is readable with no extra grant — a skill
  tells the model to copy its assets, which fails outright if it is not — while the
  neighbouring directories stay denied. It works on the real agent PVC
  (`$AGENT_ROOT/$AGENT_USER_ID/smoke`, created if missing) so the filesystem matches
  production, and says so and falls back to the default temporary directory when that
  cannot be created. Both import `generation_permission_args()` from
  `backend/domain/generation.py`, so they always exercise the profile that production
  uses — do not re-declare the profile inside these scripts.

## The Gemini route does not need this profile

This profile exists because Codex uses Bubblewrap for restricted reads and therefore needs
nested user/mount namespaces. The `google-genai` Gemini route only receives guarded Python
workspace functions and does not run a CLI or shell. A Gemini-only agent therefore runs under
**RuntimeDefault**. Keep the Localhost profile only for Pods that actually run Codex.

## Validation status

- Static allowlist/reproducibility tests: see backend/tests/test_codex_seccomp.py.
- 新しいノードへの導入は手順どおりファイルを置く。GCPではTerraformの
  起動スクリプトが同じ内容を置く（setup/gcp/terraform/startup/）。
- Docker amd64 emulation on the development Mac: namespace creation proceeds,
  but Codex reports SeccompInstall Function not implemented. Native verification
  is required; this is not a successful smoke test.
- Native k3s: the Codex permission profile persisted writes only in the dedicated
  job workspace, denied reads outside it, discarded outside writes, and denied
  direct network access without using credentials or model inference.
- Installed at `/var/lib/kubelet/seccomp/koyorina/codex-bwrap-amd64-v1.json` on
  k3s-agent-2. SHA256: `1cefea41b5b2db761e4085b4196c700159c32119d0bdc67c9cffa2b458001461`.
- Existing worker uses Localhost profile, pinned nodeSelector, UID 10001,
  drop-ALL, noNewPrivileges and its original PVC. Login remains connected.
- Temporary installer/probe Pods, ConfigMaps and NetworkPolicy have been deleted.

## Installation and future node additions

Confirm the kubelet root directory with the node administrator; do not assume it
is `/var/lib/kubelet`. Install the reviewed file beneath its `seccomp/koyorina/`
directory, root-owned and mode 0644. Use a content-versioned filename and do not
overwrite any existing profile. No kubelet restart or cluster-wide setting change
should be necessary. Target only explicitly prepared amd64 nodes; the existing
user PVC is local to k3s-agent-2.

Set only the dedicated Codex worker pod's seccompProfile to type Localhost with
localhostProfile `koyorina/codex-bwrap-amd64-v1.json`. Preserve RuntimeDefault
for the management app, controller, and Postgres. First run a credential-free
temporary probe with the same UID/capabilities/mount restrictions and no network.
Verify writes inside the job workspace plus denial of reads, persisted writes outside
the workspace, and direct network access. Then roll out the worker, preserving its PVC and login.
Do not retry a paid generation automatically or execute any generated source.

Rollback is the previous worker image/security context with the same PVC. The
inactive node profile can remain until no Pod references it. Never delete the PVC.
