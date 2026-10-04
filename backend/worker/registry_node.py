"""Synchronize one provisioned registry's transport; no runtime socket or host root access."""
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import ssl
import tomllib

import httpx

CONFIG = 'registry-pull-transport'
LABEL = 'koyorina-registry-node'
MARKER = '# Managed by Koyorina registry transport\n'


def transport_hash(host: str, http: bool) -> str:
    return hashlib.sha256(json.dumps({'host': host, 'http': http}, sort_keys=True).encode()).hexdigest()


def render_hosts(original: str, host: str, http: bool) -> str:
    if not re.fullmatch(r'[a-z0-9-]+(?:\.[a-z0-9-]+)*(?::[0-9]{2,5})?', host):
        raise ValueError('Invalid provisioned registry host')
    scheme = 'http' if http else 'https'
    if original:
        # Preserve CA/client certificate settings and other registry-specific configuration.
        tomllib.loads(original)
        rendered = re.sub(r'https?://' + re.escape(host) + r'(?=[/"\s]|$)',
                          scheme + '://' + host, original)
        if not http:
            rendered = re.sub(r'(?m)^(\s*skip_verify\s*=\s*)true\b', r'\1false', rendered)
        parsed = tomllib.loads(rendered)
        if parsed.get('server') not in (f'{scheme}://{host}', f'{scheme}://{host}/v2'):
            raise ValueError('Registry server does not match provisioned host')
        return MARKER + rendered
    endpoint = json.dumps(f'{scheme}://{host}')
    return MARKER + f'server = {endpoint}\n[host.{endpoint}]\n  capabilities = ["pull", "resolve"]\n'


def apply_transport(directory: Path, host: str, http: bool, config_file: Path, original_directory: Path | None = None) -> None:
    config = tomllib.loads(config_file.read_text())
    paths = [value.get('registry', {}).get('config_path') for value in config.get('plugins', {}).values()
             if isinstance(value, dict)]
    if '/var/lib/rancher/k3s/agent/etc/containerd/certs.d' not in paths:
        raise ValueError('k3s containerd registry config_path is not supported')
    current = directory / 'hosts.toml'
    backup = directory / '.koyorina-original-hosts.toml'
    for path in (current, backup):
        if path.is_symlink():
            raise ValueError('Registry configuration must not be a symlink')
    text = current.read_text() if current.exists() else ''
    if not text.startswith(MARKER):
        source = (original_directory / 'hosts.toml') if original_directory else current
        baseline = source.read_text() if source.exists() else ''
        backup.write_text(baseline)
        backup.chmod(0o600)
    original = backup.read_text() if backup.exists() else ''
    desired = render_hosts(original, host, http)
    if text != desired:
        temporary = directory / '.koyorina-hosts.tmp'
        if temporary.is_symlink():
            raise ValueError('Registry temporary file must not be a symlink')
        temporary.write_text(desired)
        temporary.chmod(0o644)
        temporary.replace(current)


async def run():
    host, namespace, node = [os.environ[key] for key in ('REGISTRY_HOST', 'POD_NAMESPACE', 'NODE_NAME')]
    lease = 'registry-node-' + hashlib.sha256(node.encode()).hexdigest()[:24]
    context = ssl.create_default_context(cafile='/var/run/secrets/kubernetes.io/serviceaccount/ca.crt')
    base = 'https://kubernetes.default.svc'
    ready = Path('/tmp/registry-node-ready')
    async with httpx.AsyncClient(verify=context, timeout=10, trust_env=False) as client:
        while True:
            try:
                # Kubernetes rotates the projected token while this process remains running.
                client.headers['Authorization'] = 'Bearer ' + Path('/var/run/secrets/kubernetes.io/serviceaccount/token').read_text().strip()
                response = await client.get(f'{base}/api/v1/namespaces/{namespace}/configmaps/{CONFIG}')
                response.raise_for_status()
                desired = json.loads(response.json()['data']['transport'])
                if desired['host'] != host or type(desired['http']) is not bool:
                    raise ValueError('Registry transport does not match provisioned host')
                await asyncio.to_thread(apply_transport, Path('/registry-host'), host, desired['http'],
                                        Path('/containerd-config.toml'), Path('/registry-original'))
                now = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
                body = {'apiVersion': 'coordination.k8s.io/v1', 'kind': 'Lease',
                    'metadata': {'name': lease, 'namespace': namespace,
                        'labels': {'app': LABEL}, 'annotations': {'node': node,
                        'transport-hash': transport_hash(host, desired['http'])}},
                    'spec': {'holderIdentity': node, 'leaseDurationSeconds': 30, 'renewTime': now}}
                result = await client.patch(f'{base}/apis/coordination.k8s.io/v1/namespaces/{namespace}/leases/{lease}'
                    '?fieldManager=koyorina-registry-node&force=true', json=body,
                    headers={'Content-Type': 'application/apply-patch+yaml'})
                result.raise_for_status()
                ready.touch()
            except Exception as exc:
                ready.unlink(missing_ok=True)
                # HTTP exceptions may include internal paths; never print request bodies/configuration.
                print('Registry transport synchronization failed:', str(exc) if type(exc) is ValueError else type(exc).__name__, flush=True)
            await asyncio.sleep(3)


if __name__ == '__main__':
    asyncio.run(run())
