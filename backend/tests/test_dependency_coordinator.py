import asyncio

from backend.domain.preview import dependency_digest
from backend.worker.dependency_coordinator import DependencyCoordinator


class Progress:
    def __init__(self):
        self.events = []

    def record(self, *args, **kwargs):
        self.events.append((args, kwargs))


def coordinator(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return DependencyCoordinator(workspace, tmp_path / "cache", tmp_path / "marker",
                                 object(), Progress())


def test_same_manifest_is_a_noop(tmp_path, monkeypatch):
    item = coordinator(tmp_path)
    item.marker.write_text(dependency_digest(item.workspace), encoding="utf-8")
    called = False

    async def install(*args, **kwargs):
        nonlocal called
        called = True
        return []

    monkeypatch.setattr("backend.worker.dependency_coordinator.toolchain_runner.install", install)
    result = asyncio.run(item.install())
    assert result["status"] == "unchanged"
    assert not called


def test_same_failure_twice_stops_without_a_third_install(tmp_path, monkeypatch):
    item = coordinator(tmp_path)
    calls = 0

    async def install(*args, **kwargs):
        nonlocal calls
        calls += 1
        return ["package download failed"]

    monkeypatch.setattr("backend.worker.dependency_coordinator.toolchain_runner.install", install)
    assert asyncio.run(item.install())["status"] == "failed"
    assert asyncio.run(item.install())["status"] == "stopped"
    assert asyncio.run(item.install())["status"] == "stopped"
    assert calls == 2


def test_a_job_never_installs_more_than_three_times(tmp_path, monkeypatch):
    item = coordinator(tmp_path)
    outcomes = iter((["first"], ["second"], ["third"], []))
    calls = 0

    async def install(*args, **kwargs):
        nonlocal calls
        calls += 1
        return next(outcomes)

    monkeypatch.setattr("backend.worker.dependency_coordinator.toolchain_runner.install", install)
    for _ in range(3):
        assert asyncio.run(item.install())["status"] == "failed"
    assert asyncio.run(item.install())["status"] == "stopped"
    assert calls == 3
