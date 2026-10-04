"""Provisioning regressions without a live cluster."""
import importlib.util
import json
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('publication_configure', Path(__file__).with_name('configure.py'))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_existing_secret_unchanged():
    old = {'data': {'token': 'YWJj'}, 'type': 'Opaque'}
    with patch.object(module, 'run', return_value=json.dumps(old)) as run:
        module.upsert_secret(['kubectl'], 'ns', 'name', {'token': 'abc'})
    assert run.call_count == 1


def test_secret_update_uses_resource_version_without_apply_annotation():
    old = {'data': {'token': 'b2xk'}, 'metadata': {'resourceVersion': '42'}}
    with patch.object(module, 'run', side_effect=[json.dumps(old), '']) as run:
        module.upsert_secret(['kubectl'], 'ns', 'name', {'token': 'new'})
    command, payload = run.call_args.args
    assert command == ['kubectl', 'replace', '-f', '-']
    assert json.loads(payload)['metadata']['resourceVersion'] == '42'
    assert 'annotations' not in json.loads(payload)['metadata']


def test_existing_registry_volume_preserved():
    doc = {'metadata': {'namespace': 'app-registry', 'name': 'registry-data'}}
    with patch.object(module, 'run', return_value=json.dumps({'spec': {'storageClassName': 'legacy'}})):
        assert module.preserve_registry_claim(['kubectl'], doc)
    with patch.object(module, 'run', return_value=''):
        assert not module.preserve_registry_claim(['kubectl'], doc)


def test_registry_reuses_valid_password_hash(tmp_path):
    import subprocess
    if not __import__('shutil').which('htpasswd'):
        __import__('pytest').skip('htpasswd is required on the provisioning host')
    hashed = subprocess.run(['htpasswd', '-Bni', 'builder'], input='secret\n', text=True,
                            capture_output=True, check=True).stdout
    for name in ('ca', 'cert', 'key'):
        (tmp_path / name).write_text(name)
    cfg = tmp_path / 'credentials.json'
    cfg.write_text(json.dumps({'username': 'builder', 'password': 'secret',
        'ca_file': str(tmp_path / 'ca'), 'cert_file': str(tmp_path / 'cert'), 'key_file': str(tmp_path / 'key')}))
    import base64
    def fake_run(command, payload=None):
        if command[0] == 'openssl':
            return 'public-key' if command[1] in ('x509', 'pkey') else ''
        if 'get' in command:
            return json.dumps({'data': {'htpasswd': base64.b64encode(hashed.encode()).decode()}})
        raise AssertionError(command)
    values = {'appRegistryHost': 'registry.example.com:30500', 'publication': {'internalRegistry': {'enabled': True}}}
    with patch.object(module, 'run', side_effect=fake_run), patch.object(module, 'upsert_secret') as update:
        first = module.configure_registry(['kubectl'], 'app', values, cfg)
        second = module.configure_registry(['kubectl'], 'app', values, cfg)
    assert first == second
    assert update.call_args_list[0].args[3] == {'htpasswd': hashed}


def test_http_registry_does_not_read_certificates(tmp_path):
    import subprocess
    hashed = subprocess.run(['htpasswd', '-Bni', 'builder'], input='secret\n', text=True,
                            capture_output=True, check=True).stdout
    import base64
    cfg = tmp_path / 'credentials.json'
    cfg.write_text(json.dumps({'username': 'builder', 'password': 'secret'}))
    values = {'appRegistryHost': 'registry.example.com:30500', 'publication': {
        'registryHttp': True, 'internalRegistry': {'enabled': True}}}
    old = json.dumps({'data': {'htpasswd': base64.b64encode(hashed.encode()).decode()}})
    with patch.object(module, 'run', return_value=old) as run, patch.object(module, 'upsert_secret') as update:
        module.configure_registry(['kubectl'], 'app', values, cfg)
    assert all(call.args[0][0] != 'openssl' for call in run.call_args_list)
    assert len(update.call_args_list) == 3
    assert all('tls.crt' not in call.args[3] and 'ca.crt' not in call.args[3] for call in update.call_args_list)


def test_registry_service_class_migration_only_recreates_service():
    doc = {'kind': 'Service', 'metadata': {'namespace': 'koyorina-registry', 'name': 'registry'},
           'spec': {'loadBalancerClass': 'metallb.io/l2'}}
    with patch.object(module, 'run', side_effect=[json.dumps({'spec': {}}), '']) as run:
        module.reconcile_registry_service_class(['kubectl'], doc)
    assert run.call_args.args[0] == ['kubectl', '-n', 'koyorina-registry', 'delete', 'service', 'registry', '--wait=true']
    assert run.call_count == 2


def test_matching_registry_service_class_preserved():
    doc = {'kind': 'Service', 'metadata': {'namespace': 'koyorina-registry', 'name': 'registry'},
           'spec': {'loadBalancerClass': 'metallb.io/l2'}}
    with patch.object(module, 'run', return_value=json.dumps({'spec': doc['spec']})) as run:
        module.reconcile_registry_service_class(['kubectl'], doc)
    assert run.call_count == 1


def test_removing_registry_gateway_preserves_data_and_main_gateway():
    resources = '\n'.join(['gateways.gateway.networking.k8s.io', 'httproutes.gateway.networking.k8s.io',
                           'clientsettingspolicies.gateway.nginx.org', 'nginxproxies.gateway.nginx.org'])
    with patch.object(module, 'run', side_effect=[resources, 'service/registry-nginx', '', '', '', '', '']) as run:
        module.remove_registry_gateway(['kubectl'], 'koyorina')
    commands = [call.args[0] for call in run.call_args_list]
    deletes = [command for command in commands if 'delete' in command]
    assert len(deletes) == 4
    assert all('koyorina-registry' in command for command in deletes)
    assert not any('persistentvolumeclaim' in command or 'deployment' in command for command in deletes)
    assert commands[-1] == ['kubectl', '-n', 'koyorina-registry', 'wait', '--for=delete', 'service/registry-nginx', '--timeout=120s']


def test_removing_registry_gateway_skips_clusters_without_gateway_api():
    with patch.object(module, 'run', return_value='services') as run:
        module.remove_registry_gateway(['kubectl'], 'koyorina')
    assert run.call_count == 1


def test_registry_dns_preserves_other_custom_entries():
    values = {'appRegistryHost': 'registry.internal.test:30500', 'publication': {'internalRegistry': {
        'enabled': True, 'loadBalancerIP': '192.168.110.246'}}}
    existing = {'metadata': {'resourceVersion': '42'}, 'data': {'other.server': 'unchanged'}}
    with patch.object(module, 'run', side_effect=[json.dumps(existing), '', '', '']) as run:
        module.configure_registry_dns(['kubectl'], 'koyorina', values)
    doc = json.loads(run.call_args_list[1].args[1])
    assert doc['data']['other.server'] == 'unchanged'
    assert '192.168.110.246 registry.internal.test' in doc['data']['koyorina-registry.server']
    assert doc['metadata']['resourceVersion'] == '42'
    assert run.call_args_list[1].args[0] == ['kubectl', 'replace', '-f', '-']
    assert run.call_count == 4


def test_node_transport_reapply_preserves_ui_http_selection():
    stored = {'metadata': {'resourceVersion': '7'},
        'data': {'transport': json.dumps({'host': 'registry.test:30500', 'http': True})}}
    values = {'appRegistryKind': 'private', 'appRegistryHost': 'registry.test:30500',
        'publication': {'registryHttp': False}}
    with patch.object(module, 'run', return_value=json.dumps(stored)) as call:
        module.configure_node_transport(['kubectl'], 'koyorina', values)
    assert call.call_count == 1
    values['appRegistryHost'] = 'replacement.test:30500'
    with patch.object(module, 'run', side_effect=[json.dumps(stored), '']) as call:
        module.configure_node_transport(['kubectl'], 'koyorina', values)
    command, payload = call.call_args.args
    assert command == ['kubectl', 'replace', '-f', '-']
    assert json.loads(payload)['metadata']['resourceVersion'] == '7'
    assert json.loads(json.loads(payload)['data']['transport']) == {
        'host': 'replacement.test:30500', 'http': False}
