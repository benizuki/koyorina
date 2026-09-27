"""Run with: python -m unittest discover -s setup/ansible -p test_network_audit_templates.py"""
import hashlib
import json
from pathlib import Path
import unittest

import jinja2
import yaml

ROOT = Path(__file__).resolve().parents[2]
TEMPLATES = ROOT / 'setup/ansible/roles/network-audit/templates'


class AuditTemplatesTest(unittest.TestCase):
    def render(self, template, app):
        env = jinja2.Environment(loader=jinja2.FileSystemLoader(TEMPLATES),
                                 undefined=jinja2.StrictUndefined, trim_blocks=True)
        env.filters['to_json'] = json.dumps
        env.filters['hash'] = lambda value, algorithm: hashlib.new(algorithm, value.encode()).hexdigest()
        return list(yaml.safe_load_all(env.get_template(template).render(
            forge_app_name=app,
            audit_api_host='10.10.0.3',
            audit_reader_source=list(yaml.safe_load_all(
                (ROOT / 'contrib/observability/collector.yaml').read_text(encoding='utf-8'))),
            groups={'k3s_servers': ['server']},
            hostvars={'server': {'ansible_default_ipv4': {'address': '10.0.0.2'}}})))

    def test_app_names_and_reader_contract(self):
        for app in ('ai-terakoya', 'another-app'):
            with self.subTest(app=app):
                docs = self.render('reader.yaml.j2', app)
                binding = next(d for d in docs if d['kind'] == 'RoleBinding')
                self.assertEqual(binding['subjects'][0], dict(
                    kind='ServiceAccount', name=f'{app}-viewer', namespace=app))
                ds = next(d for d in docs if d['kind'] == 'DaemonSet')
                pod = ds['spec']['template']
                self.assertEqual(pod['metadata']['labels']['app'], 'audit-collector')
                self.assertFalse(pod['spec']['automountServiceAccountToken'])
                self.assertEqual(pod['spec']['tolerations'][0]['value'], f'{app}-agent')
                self.assertTrue(all(m['readOnly'] for m in pod['spec']['containers'][0]['volumeMounts']))
                cm = next(d for d in docs if d['kind'] == 'ConfigMap')
                self.assertTrue(cm['data']['flows'].startswith('#!/bin/sh\n'))
                self.assertIn(f'/hubble/{app}*.log', cm['data']['flows'])
                policy = next(d for d in docs if d['kind'] == 'NetworkPolicy')['spec']
                self.assertEqual(policy['egress'], [])
                self.assertEqual(policy['ingress'], [])
                cilium = next(d for d in docs if d['kind'] == 'CiliumNetworkPolicy')['spec']
                self.assertEqual(cilium['endpointSelector']['matchLabels'], {'audit-reader': app})
                self.assertEqual(cilium['ingress'], [{
                    'fromEntities': ['kube-apiserver', 'host', 'remote-node'],
                    'toPorts': [{'ports': [{'port': '8081', 'protocol': 'TCP'}]}]}])

    def test_export_targets_only_generation_namespace(self):
        export = self.render('hubble.yaml.j2', 'ai-terakoya')[0]['hubble']['export']['static']
        self.assertTrue(export['enabled'])
        self.assertEqual(json.loads(export['allowList'][0]), {'source_pod': ['ai-terakoya-codex/']})
        self.assertEqual(export['filePath'], '/var/run/cilium/hubble/ai-terakoya.log')

    def test_tasks_are_valid_yaml(self):
        for relative in ('network-audit', 'gce-deploy', 'cilium'):
            tasks = yaml.safe_load((ROOT / f'setup/ansible/roles/{relative}/tasks/main.yml').read_text(encoding='utf-8'))
            self.assertIsInstance(tasks, list)

    def test_initial_install_uses_registered_internal_ip(self):
        tasks = yaml.safe_load((ROOT / 'setup/ansible/roles/cilium/tasks/main.yml').read_text(encoding='utf-8'))
        expr = next(t['ansible.builtin.set_fact']['cilium_api_addresses'] for t in tasks
                    if 'cilium_api_addresses' in t.get('ansible.builtin.set_fact', {}))
        env = jinja2.Environment(undefined=jinja2.StrictUndefined)
        env.filters['from_json'] = json.loads
        node = {'status': {'addresses': [
            {'type': 'Hostname', 'address': 'ssh-alias'},
            {'type': 'ExternalIP', 'address': '203.0.113.1'},
            {'type': 'InternalIP', 'address': '10.10.0.3'}]}}
        self.assertEqual(env.from_string(expr).render(cilium_server_node={'stdout': json.dumps(node)}),
                         "['10.10.0.3']")
        setting = next(t['ansible.builtin.set_fact']['forge_k3s_api_host'] for t in tasks
                       if 'forge_k3s_api_host' in t.get('ansible.builtin.set_fact', {}))
        self.assertEqual(env.from_string(setting).render(cilium_api_addresses=['10.10.0.3']), '10.10.0.3')

    def test_diagnostics_do_not_expose_raw_error_or_flow(self):
        tasks = yaml.safe_load((TEMPLATES.parent / 'tasks/main.yml').read_text(encoding='utf-8'))
        extract = next(t for t in tasks if t['name'] == '接続結果から安全な診断項目だけを取り出す')
        report = next(t for t in tasks if t['name'].startswith('readerの接続確認結果を表示'))
        self.assertTrue(extract['no_log'])
        self.assertFalse(report.get('no_log', False))
        self.assertEqual(report['loop'], '{{ audit_reader_diagnostics }}')
        env = jinja2.Environment(undefined=jinja2.StrictUndefined)
        reason = env.from_string(extract['vars']['audit_failure_kind']).render(
            item={'stderr': 'request timed out SECRET_TOKEN', 'stdout': 'PRIVATE_FLOW'})
        self.assertIn('Timeout', reason)
        self.assertNotIn('SECRET_TOKEN', reason)
        self.assertNotIn('PRIVATE_FLOW', reason)

    def test_api_endpoint_is_explicit_for_all_nodes(self):
        values = self.render('hubble.yaml.j2', 'ai-terakoya')[0]
        self.assertEqual(values['k8sServiceHost'], '10.10.0.3')
        self.assertEqual(values['k8sServicePort'], 6443)
        self.assertTrue(values['rollOutCiliumPods'])
        tasks = yaml.safe_load((TEMPLATES.parent / 'tasks/main.yml').read_text(encoding='utf-8'))
        upgrade = next(t for t in tasks if 'argv' in t.get('ansible.builtin.command', {}))
        self.assertEqual(upgrade['ansible.builtin.command']['argv'][:3],
                         ['/usr/local/bin/helm', 'upgrade', 'cilium'])
        # Hubbleが設定済みでも、API接続先が旧値なら差分として修復する。
        desired = next(t for t in tasks if t['name'] == 'Hubble設定の差分を判定する')
        self.assertNotIn('.hubble', desired['ansible.builtin.set_fact']['audit_hubble_desired'])


if __name__ == '__main__':
    unittest.main()
