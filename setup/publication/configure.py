"""Configure publication on an existing Koyorina k3s installation. Run on the operator's host."""
import argparse
import base64
import json
from pathlib import Path
import re
import secrets
import subprocess
import tempfile
import hashlib
import yaml

ROOT = Path(__file__).resolve().parents[2]


def run(command, payload=None):
    result = subprocess.run(command, input=payload, text=True, capture_output=True)
    if result.returncode:
        raise SystemExit('Operation failed; inspect target cluster/configuration (credential output suppressed).')
    return result.stdout


def upsert_secret(kube, namespace, name, data, secret_type='Opaque'):
    encoded = {key: base64.b64encode(value.encode()).decode() for key, value in data.items()}
    existing = run([*kube, '-n', namespace, 'get', 'secret', name, '--ignore-not-found', '-o', 'json'])
    old = json.loads(existing) if existing.strip() else None
    if old and old.get('data') == encoded and old.get('type', 'Opaque') == secret_type:
        return
    doc = {'apiVersion': 'v1', 'kind': 'Secret', 'metadata': {'name': name, 'namespace': namespace},
           'type': secret_type, 'data': encoded}
    if old:
        doc['metadata']['resourceVersion'] = old['metadata']['resourceVersion']
    run([*kube, 'replace' if old else 'create', '-f', '-'], json.dumps(doc))


def configure_registry(kube, release, values, credential_path):
    pub = values['publication']
    cfg = json.loads(credential_path.read_text())
    username, password = cfg['username'], cfg['password']
    if not re.fullmatch(r'[A-Za-z0-9_.-]+', username) or not password or '\n' in password:
        raise SystemExit('Invalid registry credentials')
    tls = not pub.get('registryHttp', False)
    ca = Path(cfg['ca_file']).read_text() if tls else ''
    internal = pub.get('internalRegistry', {})
    checksum = None
    if internal.get('enabled'):
        cert, key = '', ''
        if tls:
            cert, key = Path(cfg['cert_file']).read_text(), Path(cfg['key_file']).read_text()
            host = values['appRegistryHost'].rsplit(':', 1)[0]
            run(['openssl', 'verify', '-CAfile', cfg['ca_file'], '-verify_hostname', host, cfg['cert_file']])
            if run(['openssl', 'x509', '-in', cfg['cert_file'], '-pubkey', '-noout']) != run(
                    ['openssl', 'pkey', '-in', cfg['key_file'], '-pubout']):
                raise SystemExit('Registry certificate/key mismatch')
        ns = release + '-registry'
        auth_name = internal.get('authSecret', 'registry-auth')
        old = run([*kube, '-n', ns, 'get', 'secret', auth_name, '--ignore-not-found', '-o', 'json'])
        hashed = base64.b64decode(json.loads(old).get('data', {}).get('htpasswd', '')).decode() if old.strip() else ''
        with tempfile.NamedTemporaryFile(mode='w+', encoding='utf-8') as tmp:
            tmp.write(hashed)
            tmp.flush()
            verified = subprocess.run(['htpasswd', '-vi', tmp.name, username], input=password + '\n',
                                      text=True, capture_output=True)
        if verified.returncode:
            hashed = run(['htpasswd', '-Bni', username], password + '\n')
        upsert_secret(kube, ns, auth_name, {'htpasswd': hashed})
        if tls:
            upsert_secret(kube, ns, internal.get('tlsSecret', 'registry-tls'), {'tls.crt': cert, 'tls.key': key}, 'kubernetes.io/tls')
        checksum = hashlib.sha256((cert + key + hashed).encode()).hexdigest()
    host = values['appRegistryHost'].split('/')[0]
    auth = base64.b64encode((username + ':' + password).encode()).decode()
    docker = json.dumps({'auths': {host: {'auth': auth}}})
    for ns in [release + '-build', release + '-published']:
        upsert_secret(kube, ns, pub.get('registrySecret', 'registry-push'), {'.dockerconfigjson': docker}, 'kubernetes.io/dockerconfigjson')
    if tls:
        upsert_secret(kube, release + '-build', pub.get('registryCaSecret') or 'registry-ca', {'ca.crt': ca})
    return checksum


def preserve_registry_claim(kube, doc):
    # Existing claims may have a different class/size. Never replace or shrink stored data.
    existing = run([*kube, '-n', doc['metadata']['namespace'], 'get', 'pvc', doc['metadata']['name'],
                    '--ignore-not-found', '-o', 'json'])
    return bool(existing.strip())


def reconcile_registry_service_class(kube, doc):
    """Service class is immutable; recreate only the Registry Service, never its PVC."""
    if doc.get('kind') != 'Service' or doc['metadata']['name'] != 'registry':
        return
    namespace = doc['metadata']['namespace']
    response = run([*kube, '-n', namespace, 'get', 'service', 'registry', '--ignore-not-found', '-o', 'json'])
    if not response.strip():
        return
    current = json.loads(response)
    if current['spec'].get('loadBalancerClass') != doc['spec'].get('loadBalancerClass'):
        run([*kube, '-n', namespace, 'delete', 'service', 'registry', '--wait=true'])


def remove_registry_gateway(kube, release):
    """Release the old Gateway VIP before assigning it to the Registry Service."""
    namespace = release + '-registry'
    available = set(run([*kube, 'api-resources', '--verbs=delete', '-o', 'name']).splitlines())
    gateway = 'gateways.gateway.networking.k8s.io'
    if gateway not in available:
        return
    services = run([*kube, '-n', namespace, 'get', 'services',
                    '-l', 'gateway.networking.k8s.io/gateway-name=registry', '-o', 'name']).splitlines()
    for resource, name in [(gateway, 'registry'),
                           ('httproutes.gateway.networking.k8s.io', 'registry'),
                           ('clientsettingspolicies.gateway.nginx.org', 'registry-upload'),
                           ('nginxproxies.gateway.nginx.org', 'registry')]:
        if resource in available:
            run([*kube, '-n', namespace, 'delete', resource, name, '--ignore-not-found', '--wait=true'])
    for service in services:
        run([*kube, '-n', namespace, 'wait', '--for=delete', service, '--timeout=120s'])


def configure_registry_dns(kube, release, values):
    """Add one registry hostname through k3s's supported CoreDNS custom imports."""
    internal = values.get('publication', {}).get('internalRegistry', {})
    if not internal.get('enabled') or internal.get('serviceType', 'LoadBalancer') != 'LoadBalancer':
        return
    address = internal.get('loadBalancerIP', '')
    host = values.get('appRegistryHost', '').split(':')[0]
    import ipaddress
    try:
        ipaddress.IPv4Address(address)
    except ValueError:
        raise SystemExit('Registry DNS requires a valid dedicated IPv4 address') from None
    if not re.fullmatch(r'[a-z0-9-]+(?:\.[a-z0-9-]+)*', host):
        raise SystemExit('Registry DNS hostname is invalid')
    if host == address:
        return
    key = release + '-registry.server'
    record = (f'{host}:53 {{\n    errors\n    hosts {{\n        {address} {host}\n'
              '        ttl 30\n    }\n}\n')
    response = run([*kube, '-n', 'kube-system', 'get', 'configmap', 'coredns-custom', '--ignore-not-found', '-o', 'json'])
    existing = json.loads(response) if response.strip() else None
    if existing and existing.get('data', {}).get(key) == record:
        return
    data = dict(existing.get('data', {}) if existing else {})
    data[key] = record
    doc = {'apiVersion': 'v1', 'kind': 'ConfigMap', 'metadata': {'name': 'coredns-custom', 'namespace': 'kube-system'},
           'data': data}
    if existing:
        doc['metadata']['resourceVersion'] = existing['metadata']['resourceVersion']
    run([*kube, 'replace' if existing else 'create', '-f', '-'], json.dumps(doc))
    run([*kube, '-n', 'kube-system', 'rollout', 'restart', 'deployment/coredns'])
    run([*kube, '-n', 'kube-system', 'rollout', 'status', 'deployment/coredns', '--timeout=120s'])


def configure_node_transport(kube, release, values):
    """Keep a UI-selected protocol on reapply; initialize only new/replaced registry hosts."""
    if values.get('appRegistryKind') != 'private':
        return
    name, namespace = 'registry-pull-transport', release + '-build'
    response = run([*kube, '-n', namespace, 'get', 'configmap', name, '--ignore-not-found', '-o', 'json'])
    existing = json.loads(response) if response.strip() else None
    host = values['appRegistryHost']
    if existing and json.loads(existing['data']['transport']).get('host') == host:
        return
    document = {'apiVersion': 'v1', 'kind': 'ConfigMap',
        'metadata': {'name': name, 'namespace': namespace},
        'data': {'transport': json.dumps({'host': host, 'http': values['publication'].get('registryHttp', False)})}}
    if existing:
        document['metadata']['resourceVersion'] = existing['metadata']['resourceVersion']
    run([*kube, 'replace' if existing else 'create', '-f', '-'], json.dumps(document))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--release', default='koyorina')
    parser.add_argument('--context', required=True)
    parser.add_argument('--values', type=Path, required=True)
    parser.add_argument('--render-only', action='store_true')
    parser.add_argument('--registry-credentials', type=Path)
    args = parser.parse_args()
    if not re.fullmatch(r'[a-z](?:[a-z0-9-]{0,42}[a-z0-9])?', args.release):
        raise SystemExit('Invalid release name')
    values = yaml.safe_load(args.values.read_text())
    pub = values.get('publication', {})
    if not pub.get('enabled'):
        raise SystemExit('publication.enabled=true is required')
    rendered = run(['helm', 'template', args.release, str(Path(__file__).resolve().parents[1] / 'helm/koyorina'),
        '-f', str(args.values), '--show-only', 'templates/publication.yaml',
        *(['--show-only', 'templates/publication-registry-node.yaml'] if values.get('appRegistryKind') == 'private' else []),
        *(['--show-only', 'templates/publication-registry.yaml'] if pub.get('internalRegistry', {}).get('enabled') else [])])
    if args.render_only:
        print(rendered)
        return
    kube = ['kubectl', '--context', args.context]
    docs = [d for d in yaml.safe_load_all(rendered) if d]
    # Namespace first; no image jobs or application workloads are submitted here.
    for doc in docs:
        if doc['kind'] == 'Namespace':
            run([*kube, 'apply', '-f', '-'], json.dumps(doc))
    registry_checksum = None
    if args.registry_credentials:
        registry_checksum = configure_registry(kube, args.release, values, args.registry_credentials)
    def token(namespace, name, key):
        response = run([*kube, '-n', namespace, 'get', 'secret', name, '--ignore-not-found', '-o', 'json'])
        if not response.strip():
            return None
        data = json.loads(response).get('data', {})
        if key not in data:
            raise SystemExit('Existing publication credential is malformed; no overwrite performed.')
        return base64.b64decode(data[key]).decode()
    build_ns = args.release + '-build'
    configure_node_transport(kube, args.release, values)
    pairs = [(build_ns, 'publication-controller-auth', 'token'),
             (args.release, args.release + '-publication-client', 'PUBLICATION_CONTROLLER_TOKEN')]
    existing = [token(*item) for item in pairs]
    found = [value for value in existing if value]
    if found and (len(set(found)) != 1 or len(found[0]) < 32):
        raise SystemExit('Existing publication credentials differ or are too short; no overwrite performed.')
    shared = found[0] if found else secrets.token_urlsafe(48)
    for (ns, name, key), old in zip(pairs, existing):
        if not old:
            run([*kube, 'create', '-f', '-'], json.dumps({'apiVersion': 'v1', 'kind': 'Secret',
                'metadata': {'name': name, 'namespace': ns}, 'stringData': {key: shared}}))
    internal = pub.get('internalRegistry', {})
    if internal.get('enabled'):
        remove_registry_gateway(kube, args.release)
    for doc in docs:
        if doc['kind'] != 'Namespace':
            if doc['kind'] == 'PersistentVolumeClaim' and preserve_registry_claim(kube, doc):
                continue
            if registry_checksum and doc['kind'] == 'Deployment' and doc['metadata']['name'] == 'registry':
                doc['spec']['template'].setdefault('metadata', {}).setdefault('annotations', {})['koyorina.io/registry-config'] = registry_checksum
            reconcile_registry_service_class(kube, doc)
            run([*kube, 'apply', '-f', '-'], json.dumps(doc))
    internal = pub.get('internalRegistry', {})
    if internal.get('enabled') and internal.get('serviceType', 'LoadBalancer') == 'LoadBalancer' and internal.get('loadBalancerIP'):
        target = 'service/registry'
        address = '.status.loadBalancer.ingress[0].ip'
        run([*kube, '-n', args.release + '-registry', 'wait', target,
             '--for=jsonpath={' + address + '}=' + internal['loadBalancerIP'], '--timeout=120s'])
    configure_registry_dns(kube, args.release, values)
    # Legacy manifest installations use the same configuration as the Helm deployment.
    env = [{'name': 'PUBLICATION_ENABLED', 'value': 'true'},
        {'name': 'PUBLICATION_CONTROLLER_URL', 'value': f'http://{args.release}-publish-controller.{build_ns}.svc:8080'},
        {'name': 'PUBLICATION_CONTROLLER_TOKEN', 'valueFrom': {'secretKeyRef': {
            'name': args.release + '-publication-client', 'key': 'PUBLICATION_CONTROLLER_TOKEN'}}},
        {'name': 'APP_REGISTRY_KIND', 'value': values['appRegistryKind']},
        {'name': 'APP_REGISTRY_HOST', 'value': values['appRegistryHost']},
        {'name': 'APP_REGISTRY_SCANNING_ENABLED', 'value': str(pub.get('scanningEnabled', False)).lower()}]
    run([*kube, '-n', args.release, 'patch', 'deployment', args.release, '--type=strategic', '--patch-file=/dev/stdin'],
        json.dumps({'spec': {'template': {'spec': {'containers': [{'name': 'app', 'env': env}]}}}}))
    print('Publication resources configured. Run preflight and smoke tests before publishing an app.')


if __name__ == '__main__':
    main()
