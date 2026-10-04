import hashlib
import importlib.util
import io
import json
from pathlib import Path
from unittest.mock import patch
import pytest

spec = importlib.util.spec_from_file_location('resolve_images', Path(__file__).with_name('resolve_images.py'))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class Response(io.BytesIO):
    def __init__(self, data, headers=None):
        super().__init__(data)
        self.headers = headers or {}


def test_digest_override_needs_no_network():
    image = 'private.example/tools/buildkit@sha256:' + 'a' * 64
    with patch.object(module, 'urlopen') as request:
        assert module.resolve(image) == image
    request.assert_not_called()


@pytest.mark.parametrize('image,repository', [('python:3.14-slim', 'library/python'),
    ('moby/buildkit:v0.25.2-rootless', 'moby/buildkit')])
def test_resolves_manifest_index_and_verifies_digest(image, repository):
    content = b'{"mediaType":"application/vnd.oci.image.index.v1+json","manifests":[]}'
    digest = 'sha256:' + hashlib.sha256(content).hexdigest()
    with patch.object(module, 'urlopen', side_effect=[Response(b'{"token":"short-lived"}'),
         Response(content, {'Docker-Content-Digest': digest})]) as request:
        assert module.resolve(image) == f'docker.io/{repository}@{digest}'
    assert request.call_args.args[0].full_url.startswith('https://registry-1.docker.io/v2/' + repository)


def test_rejects_manifest_digest_mismatch():
    with patch.object(module, 'urlopen', side_effect=[Response(b'{"token":"short-lived"}'),
         Response(b'changed', {'Docker-Content-Digest': 'sha256:' + 'a' * 64})]):
        with pytest.raises(ValueError, match='一致'):
            module.resolve('python:3.14-slim')


def test_empty_settings_use_defaults(capsys):
    with patch('sys.argv', ['resolve_images', '--buildkit', '', '--helper', '']), \
         patch.object(module, 'resolve', side_effect=['build-digest', 'helper-digest']) as resolve:
        module.main()
    assert [call.args[0] for call in resolve.call_args_list] == [module.DEFAULT_BUILDKIT, module.DEFAULT_HELPER]
    assert json.loads(capsys.readouterr().out)['buildkit'] == 'build-digest'
