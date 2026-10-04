"""Registry storage rendering; independent from published SQLite storage."""
from pathlib import Path
import shutil
import subprocess
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]


def render(storage_class=None, access_mode=None, template="publication-registry.yaml", **overrides):
    if not shutil.which('helm'):
        pytest.skip('helm is required')
    command = ['helm', 'template', 'koyorina', str(ROOT / 'setup/helm/koyorina'),
               '--show-only', 'templates/' + template]
    values = {'publication.enabled': 'true', 'publication.internalRegistry.enabled': 'true',
              'appRegistryKind': 'private', 'appRegistryHost': 'registry.example.com:30500',
              'publication.buildkitImage': 'moby/buildkit@sha256:' + 'a' * 64,
              'publication.helperImage': 'python@sha256:' + 'b' * 64,
              'publication.kubernetesApiCidr': '192.168.110.70/32',
              'publication.registryCidr': '192.168.110.246/32'}
    if storage_class is not None:
        values['publication.internalRegistry.storageClass'] = storage_class
    if access_mode is not None:
        values['publication.internalRegistry.accessMode'] = access_mode
    values.update(overrides)
    for key, value in values.items():
        command += ['--set', key + '=' + value]
    return subprocess.run(command, capture_output=True, text=True)


@pytest.mark.parametrize('storage,mode', [(None, None), ('cephfs', 'ReadWriteMany')])
def test_registry_storage_selection(storage, mode):
    result = render(storage, mode)
    assert result.returncode == 0, result.stderr
    docs = [d for d in yaml.safe_load_all(result.stdout) if d]
    claim = next(d for d in docs if d['kind'] == 'PersistentVolumeClaim')
    assert claim['spec']['storageClassName'] == (storage or 'local-path')
    assert claim['spec']['accessModes'] == [mode or 'ReadWriteOnce']
    deployment = next(d for d in docs if d['kind'] == 'Deployment')
    assert deployment['spec']['replicas'] == 1


@pytest.mark.parametrize('storage,mode', [('local-path', 'ReadWriteMany'), ('cephfs', 'ReadOnlyMany')])
def test_invalid_storage_combination_rejected(storage, mode):
    result = render(storage, mode)
    assert result.returncode != 0
    assert 'Registry' in result.stderr


@pytest.mark.parametrize('service_type', ['LoadBalancer', 'NodePort'])
def test_registry_endpoint_matches_service_mode(service_type):
    result = render(**{'publication.internalRegistry.serviceType': service_type,
                       'publication.internalRegistry.loadBalancerIP': '192.168.110.246'})
    assert result.returncode == 0, result.stderr
    service = next(d for d in yaml.safe_load_all(result.stdout) if d and d['kind'] == 'Service')
    spec = service['spec']
    assert spec['type'] == service_type
    port = spec['ports'][0]
    assert port['targetPort'] == 5000
    if service_type == 'LoadBalancer':
        assert spec['loadBalancerIP'] == '192.168.110.246'
        assert spec['loadBalancerClass'] == 'metallb.io/l2'
        assert port['port'] == 30500
        assert 'nodePort' not in port
    else:
        assert 'loadBalancerClass' not in spec
        assert 'loadBalancerIP' not in spec
        assert port['port'] == 5000 and port['nodePort'] == 30500


@pytest.mark.parametrize('http', ['true', 'false'])
def test_registry_is_published_directly_without_gateway(http):
    result = render(**{'publication.internalRegistry.loadBalancerIP': '192.168.110.246',
                       'publication.registryHttp': http})
    assert result.returncode == 0, result.stderr
    docs = {d['kind']: d for d in yaml.safe_load_all(result.stdout) if d}
    assert not {'Gateway', 'HTTPRoute', 'NginxProxy', 'ClientSettingsPolicy'} & docs.keys()
    assert docs['Service']['spec']['type'] == 'LoadBalancer'
    assert docs['Service']['spec']['loadBalancerIP'] == '192.168.110.246'
    env = docs['Deployment']['spec']['template']['spec']['containers'][0]['env']
    assert any(e['name'].startswith('REGISTRY_HTTP_TLS') for e in env) == (http == 'false')


@pytest.mark.parametrize('cilium', ['true', 'false'])
def test_controller_api_access_on_cilium_is_limited(cilium):
    result = render(template='publication.yaml', **{'publication.ciliumEnabled': cilium})
    assert result.returncode == 0, result.stderr
    policies = [d for d in yaml.safe_load_all(result.stdout) if d and d['kind'] == 'CiliumNetworkPolicy']
    assert len(policies) == (1 if cilium == 'true' else 0)
    if policies:
        spec = policies[0]['spec']
        assert spec['endpointSelector']['matchLabels'] == {'app': 'publication-controller'}
        assert spec['egress'] == [{'toEntities': ['kube-apiserver'], 'toPorts': [{'ports': [
            {'port': '443', 'protocol': 'TCP'}, {'port': '6443', 'protocol': 'TCP'}]}]}]


def test_build_jobs_can_reach_http_package_mirrors():
    result = render(template='publication.yaml')
    assert result.returncode == 0, result.stderr
    policies = {doc['metadata']['name']: doc for doc in yaml.safe_load_all(result.stdout)
                if doc and doc['kind'] == 'NetworkPolicy'}
    egress = policies['builds']['spec']['egress']
    public = next(rule for rule in egress if rule.get('to') == [{'ipBlock': {
        'cidr': '0.0.0.0/0', 'except': ['10.0.0.0/8', '172.16.0.0/12',
            '192.168.0.0/16', '169.254.0.0/16']}}])
    assert {port['port'] for port in public['ports']} == {80, 443}


def test_publication_starts_without_ansible_registry_selection():
    result = render(template='publication.yaml', **{
        'publication.internalRegistry.enabled': 'false',
        'appRegistryKind': 'unconfigured', 'appRegistryHost': '', 'publication.registryCidr': ''})
    assert result.returncode == 0, result.stderr
    docs = [doc for doc in yaml.safe_load_all(result.stdout) if doc]
    controller = next(doc for doc in docs if doc['kind'] == 'Deployment')
    env = {item['name']: item['value'] for item in controller['spec']['template']['spec']['containers'][0]['env']
           if 'value' in item}
    assert env['PUBLICATION_CONTROLLER_REGISTRY_KIND'] == 'unconfigured'
    assert env['PUBLICATION_CONTROLLER_REGISTRY_HOST'] == ''


def test_registry_node_has_only_scoped_host_files_and_readonly_api_permissions():
    result = render(template='publication-registry-node.yaml')
    assert result.returncode == 0, result.stderr
    docs = [doc for doc in yaml.safe_load_all(result.stdout) if doc]
    daemon = next(doc for doc in docs if doc['kind'] == 'DaemonSet')
    pod = daemon['spec']['template']['spec']
    assert not pod.get('hostNetwork') and not pod.get('hostPID')
    host_paths = {v['name']: v['hostPath']['path'] for v in pod['volumes'] if 'hostPath' in v}
    assert host_paths['registry-host'].endswith('/registry.example.com_30500_')
    assert host_paths['registry-original'].endswith('/registry.example.com:30500')
    assert host_paths['containerd-config'].endswith('/containerd/config.toml')
    container = pod['containers'][0]
    assert '@sha256:' in container['image']
    assert not container['securityContext'].get('privileged')
    assert container['securityContext']['capabilities']['drop'] == ['ALL']
    assert next(v for v in container['volumeMounts'] if v['name'] == 'registry-original')['readOnly']
    role = next(doc for doc in docs if doc['kind'] == 'Role')
    assert role['rules'][0]['resourceNames'] == ['registry-pull-transport']
    assert all('secrets' not in rule['resources'] and 'pods' not in rule['resources'] for rule in role['rules'])
