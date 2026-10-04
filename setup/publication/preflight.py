"""Read-only cluster checks plus optional node cache pull test (operator invoked)."""
import argparse
import json
import shlex
import subprocess
import sys
from pathlib import Path


def command(args):
    result = subprocess.run(args, capture_output=True, text=True, timeout=90)
    if result.returncode:
        raise RuntimeError('Command failed (credential-bearing output suppressed)')
    return result.stdout


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--context', required=True)
    parser.add_argument('--release', default='koyorina')
    parser.add_argument('--registry-kind', choices=['private', 'artifact'], required=True)
    parser.add_argument('--registry-host', required=True)
    parser.add_argument('--node', action='append', default=[], help='SSH destination for each eligible build/runtime node')
    parser.add_argument('--pull-image', help='Known image with digest; pulled via node containerd to verify TLS/auth')
    args = parser.parse_args()
    failures = []
    kube = ['kubectl', '--context', args.context]
    def check(label, action):
        try:
            action()
            print('OK:', label)
        except Exception:
            failures.append(label)
            print('FAILED:', label)
    def storage():
        sc = json.loads(command([*kube, 'get', 'storageclass', 'local-path', '-o', 'json']))
        assert sc['provisioner'] == 'rancher.io/local-path', 'SQLite cannot use NFS'
    check('local-path storage provisioner', storage)
    for ns in ('build', 'published'):
        check(ns + ' namespace', lambda ns=ns: command([*kube, 'get', 'namespace', args.release + '-' + ns]))
    def version():
        v = json.loads(command([*kube, 'version', '-o', 'json']))['serverVersion']
        assert int(v['major']) == 1 and int(v['minor'].rstrip('+')) >= 30
    check('Kubernetes >= 1.30 (AppArmor field)', version)
    if args.registry_kind == 'private' and '.svc' in args.registry_host:
        failures.append('registry hostname must resolve from nodes')
    if args.pull_image and not args.pull_image.startswith(args.registry_host + '/'):
        raise SystemExit('--pull-image must be in the configured registry')
    for node in args.node:
        def namespaces(node=node):
            value = command(['ssh', node, 'sysctl -n user.max_user_namespaces']).strip()
            assert int(value) > 0
            # Some distributions expose this switch; absent means user namespaces aren't gated here.
            value = command(['ssh', node, "if [ -e /proc/sys/kernel/unprivileged_userns_clone ]; then cat /proc/sys/kernel/unprivileged_userns_clone; else echo 1; fi"]).strip()
            assert value == '1'
        check(node + ' rootless user namespaces', namespaces)
        if args.pull_image:
            check(node + ' containerd TLS/auth pull', lambda node=node: command(
                ['ssh', node, 'sudo k3s crictl pull ' + shlex.quote(args.pull_image)]))
    if not args.node:
        failures.append('no node SSH checks requested')
    if not args.pull_image:
        failures.append('no containerd pull image specified')
    if failures:
        print('Preflight incomplete:', ', '.join(failures))
        return 1
    print('Preflight passed. A full build/publish smoke test is still required.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
