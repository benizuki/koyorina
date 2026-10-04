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


def test_publication_namespace_scope_is_explicit():
    assert cluster.app_namespaces('koyorina', True) == cluster.NAMESPACES + (
        'koyorina-build', 'koyorina-published')
    with pytest.raises(ValueError):
        cluster.app_namespaces('../kube-system', True)


def test_published_runtime_reads_only_its_labeled_pods():
    paths = []
    def respond(request):
        paths.append(str(request.url))
        if '/deployments/' in request.url.path:
            return httpx.Response(404)
        return httpx.Response(200, json={'items': []})
    async def check():
        client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        with patch.object(cluster.TOKEN_PATH.__class__, 'read_text', return_value='test-token'), \
             patch.object(cluster.ssl, 'create_default_context'), \
             patch.object(cluster.httpx, 'AsyncClient', return_value=client):
            result = await cluster.read_published('koyorina-published', 'published-abc')
        assert result == {'pods': [], 'deployment': None}
        assert 'labelSelector=koyorina-published%3Dpublished-abc' in paths[0]
        with pytest.raises(ValueError):
            await cluster.read_published('kube-system', '../other')
    asyncio.run(check())


def test_pull_failure_is_visible_without_leaking_secrets():
    item = {'metadata': {'name': 'published-test'}, 'spec': {'containers': [{}]},
            'status': {'phase': 'Pending', 'containerStatuses': [
                {'state': {'waiting': {'reason': 'ImagePullBackOff',
                 'message': 'HTTP response to HTTPS client token=private'}}}]}}
    view = cluster.pod_view(item)
    assert view['reason'] == 'ImagePullBackOff'
    assert view['message'] == 'HTTP response to HTTPS client token=***'
    assert cluster.pod_problem({'status': {'conditions': [
        {'type': 'PodScheduled', 'status': 'False', 'reason': 'Unschedulable',
         'message': 'volume node affinity conflict'}]}})[0] == 'Unschedulable'


def test_pod_identity_labels_are_extracted_for_admin_display():
    project = '12345678-1234-4234-8234-123456789abc'
    pod = {'metadata': {'name': 'preview-pod', 'labels': {
        'app.kubernetes.io/name': 'preview-' + project.replace('-', ''),
        'forge-user': '98765432-1234-4234-8234-123456789abc',
        'forge-tenant': 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'}},
        'spec': {'containers': [{}]}, 'status': {'phase': 'Pending'}}
    view = cluster.pod_view(pod)
    assert view['project_id'] == project
    assert view['user_id'] == '98765432-1234-4234-8234-123456789abc'
    assert view['tenant_id'] == 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'


def test_pod_details_include_scheduling_and_events_without_secrets():
    paths = []
    def respond(request):
        paths.append(str(request.url))
        if request.url.path.endswith('/events'):
            return httpx.Response(200, json={'items': [{
                'type': 'Warning', 'reason': 'FailedScheduling',
                'message': '0/3 nodes available; token=hidden', 'count': 2,
                'lastTimestamp': '2026-10-03T00:00:00Z'}]})
        return httpx.Response(200, json={'metadata': {'name': 'preview-pod'},
            'spec': {'containers': [{}]}, 'status': {'phase': 'Pending',
                'conditions': [{'type': 'PodScheduled', 'status': 'False',
                    'reason': 'Unschedulable', 'message': 'volume conflict'}]}})
    async def check():
        client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        with patch.object(cluster.TOKEN_PATH.__class__, 'read_text', return_value='test-token'), \
             patch.object(cluster.ssl, 'create_default_context'), \
             patch.object(cluster.httpx, 'AsyncClient', return_value=client):
            result = await cluster.read_pod_details('koyorina-preview', 'preview-pod',
                namespaces=cluster.NAMESPACES)
        assert result['conditions'][0]['reason'] == 'Unschedulable'
        assert result['events'][0]['message'] == '0/3 nodes available; token=***'
        assert 'fieldSelector=' in paths[1]
        with pytest.raises(ValueError):
            await cluster.read_pod_details('kube-system', 'preview-pod', namespaces=cluster.NAMESPACES)
    asyncio.run(check())
