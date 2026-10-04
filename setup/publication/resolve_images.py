"""Resolve infrastructure image tags through the Docker Hub API; no Docker daemon needed."""
import argparse
import hashlib
import json
import re
from urllib.parse import urlencode
from urllib.request import Request, urlopen

DEFAULT_BUILDKIT = 'moby/buildkit:v0.25.2-rootless'
DEFAULT_HELPER = 'python:3.14-slim'
ACCEPT = ', '.join(['application/vnd.oci.image.index.v1+json',
    'application/vnd.docker.distribution.manifest.list.v2+json',
    'application/vnd.oci.image.manifest.v1+json', 'application/vnd.docker.distribution.manifest.v2+json'])


def resolve(image):
    if re.fullmatch(r'[a-zA-Z0-9./:_-]+@sha256:[a-f0-9]{64}', image):
        return image
    match = re.fullmatch(r'(?:docker.io/)?([a-z0-9_-]+(?:/[a-z0-9_.-]+)?):([A-Za-z0-9_.-]+)', image)
    if not match:
        raise ValueError('イメージはDocker Hubのタグ、または完全なdigestで指定してください。')
    repository, tag = match.groups()
    if '/' not in repository:
        repository = 'library/' + repository
    query = urlencode({'service': 'registry.docker.io', 'scope': f'repository:{repository}:pull'})
    with urlopen('https://auth.docker.io/token?' + query, timeout=30) as response:
        token = json.load(response)['token']
    request = Request(f'https://registry-1.docker.io/v2/{repository}/manifests/{tag}',
                      headers={'Authorization': 'Bearer ' + token, 'Accept': ACCEPT})
    with urlopen(request, timeout=30) as response:
        content = response.read()
        digest = response.headers.get('Docker-Content-Digest', '')
    if digest != 'sha256:' + hashlib.sha256(content).hexdigest():
        raise ValueError('Registryから取得したmanifestのdigestが一致しません。')
    return f'docker.io/{repository}@{digest}'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--buildkit', default=DEFAULT_BUILDKIT)
    parser.add_argument('--helper', default=DEFAULT_HELPER)
    args = parser.parse_args()
    try:
        result = {'buildkit': resolve(args.buildkit or DEFAULT_BUILDKIT),
                  'helper': resolve(args.helper or DEFAULT_HELPER)}
    except Exception as exc:
        raise SystemExit('ビルド基盤イメージの版を取得できません。Docker HubへのHTTPS接続を確認するか、'
                         'forge_publication_buildkit_image/helper_imageにdigestを指定してください。') from exc
    print(json.dumps(result))


if __name__ == '__main__':
    main()
