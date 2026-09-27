"""クラスタの状態表示。読むだけで、見せる範囲も決め打ちにする。"""
from backend.core import cluster
import asyncio
from unittest.mock import patch
import httpx
import pytest


def test_pod_view_shows_what_the_screen_needs():
    item = {"metadata": {"name": "koyorina-1", "creationTimestamp": "2026-09-13T00:00:00Z"},
            "spec": {"nodeName": "k3s-agent-2",
                     "containers": [{"image": "registry.example.com/koyorina@sha256:" + "a" * 64}]},
            "status": {"phase": "Running",
                       "containerStatuses": [{"ready": True, "restartCount": 2,
                                              "image": "registry.example.com/koyorina"}]}}
    view = cluster.pod_view(item)
    assert view["phase"] == "Running" and view["ready"] == 1 and view["restarts"] == 2
    assert view["node"] == "k3s-agent-2"
    # digestは先頭だけ。どの版かが分かれば足りる。
    assert view["images"] == ["koyorina@" + "a" * 12]


def test_only_the_three_namespaces_are_read():
    assert cluster.NAMESPACES == ("koyorina", "koyorina-codex", "koyorina-preview")


def test_renamed_namespace_requests_and_log_scope():
    namespaces = cluster.app_namespaces("ai-terakoya")
    assert namespaces == ("ai-terakoya", "ai-terakoya-codex", "ai-terakoya-preview")
    paths = []
    def respond(request):
        paths.append(request.url.path)
        return httpx.Response(200, json={"items": []})
    async def check():
        client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        with patch.object(cluster.TOKEN_PATH.__class__, "read_text", return_value="test-token"), \
             patch.object(cluster.ssl, "create_default_context"), \
             patch.object(cluster.httpx, "AsyncClient", return_value=client):
            result = await cluster.read(namespaces)
        assert [n["namespace"] for n in result["namespaces"]] == list(namespaces)
        for namespace in namespaces:
            assert f"/api/v1/namespaces/{namespace}/pods" in paths
        with pytest.raises(ValueError):
            await cluster.read_logs("koyorina", "pod-1", namespaces=namespaces)
        with pytest.raises(ValueError):
            await cluster.read_logs("kube-system", "pod-1", namespaces=namespaces)
    asyncio.run(check())


@pytest.mark.parametrize("status", [403, 404, 500])
def test_cluster_read_failures_are_not_empty_success(status):
    async def check():
        client = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(status)))
        with patch.object(cluster.TOKEN_PATH.__class__, "read_text", return_value="test-token"), \
             patch.object(cluster.ssl, "create_default_context"), \
             patch.object(cluster.httpx, "AsyncClient", return_value=client):
            with pytest.raises(httpx.HTTPStatusError):
                await cluster.read(cluster.app_namespaces("ai-terakoya"))
    asyncio.run(check())


def test_log_lines_remove_terminal_codes_and_common_secret_values():
    line = cluster.clean_log_line(
        "\x1b[31mERROR token=secret-value password: another Authorization: Bearer abc123\x1b[0m")
    assert line == "ERROR token=*** password: *** Authorization: ***"


def test_service_view_keeps_ports_readable():
    item = {"metadata": {"name": "koyorina"},
            "spec": {"type": "ClusterIP", "clusterIP": "10.43.0.1",
                     "ports": [{"port": 8080, "targetPort": "http"}]}}
    assert cluster.service_view(item)["ports"] == ["8080→http"]


def test_registry_choice_is_validated_at_startup():
    """設定の書き間違いは、押し込む時点ではなく配備の時点で見つける。"""
    import pytest
    from backend.domain import app_images
    app_images.validate("artifact", "asia-northeast1-docker.pkg.dev/example-project-dev/koyorina-apps")
    app_images.validate("private", "registry.koyorina-registry.svc:5000")
    for kind, host in (("artifact", "registry.example.com"), ("private", "http://registry"),
                       ("both", "registry.koyorina-registry.svc:5000"), ("private", "")):
        with pytest.raises(ValueError):
            app_images.validate(kind, host)


def test_one_tag_per_generated_artifact():
    """同じタグを上書きしない。上書きすると、動いていた版へ戻せなくなる。"""
    from backend.domain import app_images
    first = app_images.reference("private", "registry.koyorina-registry.svc:5000", "p1", "a" * 32)
    second = app_images.reference("private", "registry.koyorina-registry.svc:5000", "p1", "b" * 32)
    assert first != second and first.startswith("registry.koyorina-registry.svc:5000/p1:")
