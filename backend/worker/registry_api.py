"""Docker Registry HTTP API v2 で、ビルドしたイメージを消す。

内部Registry（distribution）とArtifact Registryの両方が同じAPIを話す。消すのは
そのイメージのリポジトリ（＝プロジェクト）の中だけで、URLに含めるリポジトリ名は
ビルド時に記録した参照からしか作らない。

同じ中身でビルドし直すと、別のビルドでもダイジェストが一致する（実際に起きている）。
マニフェストはダイジェスト単位で消え、指していたタグもまとめて消えるので、残すビルドと
同じダイジェストなら消さない（keep）。
"""
import re

MANIFEST_ACCEPT = ", ".join((
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.docker.distribution.manifest.v2+json"))
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
# 結果。deleted/missing/shared は「もう片付けなくてよい」、forbidden/failed はやり直す。
SETTLED = {"deleted", "missing", "shared"}


def split_image(image: str) -> tuple[str, str, str]:
    """`host[:port]/path/repo:tag` を (host, path/repo, tag) に分ける。"""
    endpoint, _, rest = image.partition("/")
    repository, _, tag = rest.rpartition(":")
    if not endpoint or not repository or not tag or "@" in rest or ".." in repository:
        raise ValueError("unexpected image reference")
    return endpoint, repository, tag


async def remove(client, base_url: str, headers: dict, image: str, digest: str | None,
                 keep: set[str]) -> str:
    """1つのビルドのイメージを消す。ダイジェストが分からなければタグから引く。"""
    _, repository, tag = split_image(image)
    url = f"{base_url}/v2/{repository}/manifests/"
    if not digest:
        found = await client.head(url + tag, headers={**headers, "Accept": MANIFEST_ACCEPT})
        if found.status_code == 404:
            return "missing"
        if found.status_code in (401, 403):
            return "forbidden"
        if not found.is_success:
            return "failed"
        digest = found.headers.get("Docker-Content-Digest", "")
    if not DIGEST.fullmatch(digest or ""):
        return "failed"
    if digest in keep:
        return "shared"
    response = await client.delete(url + digest, headers=headers)
    if response.status_code in (200, 202):
        return "deleted"
    if response.status_code == 404:
        return "missing"
    if response.status_code in (401, 403):
        return "forbidden"
    # 405 は削除が無効なRegistry（REGISTRY_STORAGE_DELETE_ENABLED=false）。
    return "failed"
