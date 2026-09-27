"""One policy boundary for model-requested dependency installation.

Models never choose a command, package, path, or registry.  They can only ask the
coordinator to validate the manifests in the current project and reconcile them.
"""
from dataclasses import dataclass, field
from pathlib import Path

from backend.domain.preview import dependency_digest
from backend.worker import toolchain_runner


@dataclass
class DependencyCoordinator:
    workspace: Path
    cache: Path
    marker: Path
    source: object
    progress: object
    max_attempts: int = 3
    attempts: int = 0
    failures: list[str] = field(default_factory=list)

    async def install(self) -> dict:
        digest = dependency_digest(self.workspace)
        installed = self.marker.read_text(encoding="utf-8").strip() if self.marker.is_file() else ""
        if installed == digest:
            self.progress.record("status", "部品は前回と同じ構成なので、そのまま使います。")
            return {"status": "unchanged", "digest": digest, "problems": []}
        if len(self.failures) >= 2 and self.failures[-1] == self.failures[-2]:
            return {"status": "stopped", "digest": digest,
                    "problems": ["同じ依存導入エラーが続いたため停止しました。"],
                    "latched": True}
        if self.attempts >= self.max_attempts:
            return {"status": "stopped", "digest": digest,
                    "problems": ["このジョブでの依存導入回数が上限に達しました。"],
                    "latched": True}
        self.attempts += 1
        problems = await toolchain_runner.install(
            self.workspace, self.source, cache=self.cache,
            report=lambda message, failed=False: self.progress.record(
                "command", message, key="install", state="failed" if failed else "running"))
        signature = "\n".join(problems)
        if problems:
            self.failures.append(signature)
            self.progress.record("command", "一部の部品が取得できませんでした。",
                                 key="install", state="failed")
            repeated = len(self.failures) >= 2 and self.failures[-1] == self.failures[-2]
            return {"status": "stopped" if repeated else "failed", "digest": digest,
                    "problems": problems, "retryable": not repeated, "latched": False}
        self.marker.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.marker.write_text(digest, encoding="utf-8")
        self.progress.record("command", "部品がそろいました。", key="install", state="done")
        return {"status": "installed", "digest": digest, "problems": []}
