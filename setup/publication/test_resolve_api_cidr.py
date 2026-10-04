import importlib.util
from pathlib import Path
from unittest.mock import patch
import pytest

spec = importlib.util.spec_from_file_location('resolve_api_cidr', Path(__file__).with_name('resolve_api_cidr.py'))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


@pytest.mark.parametrize('server,expected', [('https://192.168.110.70:6443', '192.168.110.70/32'),
    ('https://[fd00::10]:6443', 'fd00::10/128')])
def test_ip_endpoint(server, expected):
    assert module.resolve(server) == expected


def test_dns_endpoint():
    with patch.object(module.socket, 'getaddrinfo', return_value=[(2, 1, 6, '', ('192.168.110.70', 0))]):
        assert module.resolve('https://k3s.example.com:6443') == '192.168.110.70/32'


@pytest.mark.parametrize('server', ['https://127.0.0.1:6443', 'https://0.0.0.0:6443', 'invalid'])
def test_unreachable_endpoint_rejected(server):
    with pytest.raises(ValueError):
        module.resolve(server)


def test_multiple_addresses_require_explicit_configuration():
    with patch.object(module.socket, 'getaddrinfo', return_value=[(2, 1, 6, '', ('192.168.110.70', 0)),
         (2, 1, 6, '', ('192.168.110.71', 0))]):
        with pytest.raises(ValueError, match='明示'):
            module.resolve('https://k3s.example.com:6443')


def test_registry_endpoint():
    with patch.object(module.socket, 'getaddrinfo', return_value=[(2, 1, 6, '', ('192.168.110.70', 0))]):
        assert module.resolve('http://registry.benizuki.local:30500', registry=True) == '192.168.110.70/32'


def test_registry_multiple_addresses_names_correct_setting():
    with patch.object(module.socket, 'getaddrinfo', return_value=[(2, 1, 6, '', ('192.168.110.70', 0)),
         (2, 1, 6, '', ('192.168.110.71', 0))]):
        with pytest.raises(ValueError, match='forge_publication_registry_cidr'):
            module.resolve('http://registry.example.com:30500', registry=True)
