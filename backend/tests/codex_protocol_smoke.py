"""Run in the pinned image with --network none. No credentials or inference used."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
from backend.domain.generation import generation_permission_args


def main():
    with tempfile.TemporaryDirectory(prefix="forge-protocol-") as directory:
        root = Path(directory)
        (root / "codex").mkdir()
        workspace = root / "workspace"
        workspace.mkdir()
        env = {"PATH": os.environ["PATH"], "HOME": directory, "CODEX_HOME": str(root / "codex")}
        process = subprocess.Popen(["codex", "app-server", "--listen", "stdio://", "-c", 'cli_auth_credentials_store="file"',
                                    *generation_permission_args(workspace)],
            cwd=root, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        def call(identifier, method, params):
            process.stdin.write(json.dumps({"id": identifier, "method": method, "params": params}) + "\n")
            process.stdin.flush()
            while line := process.stdout.readline():
                result = json.loads(line)
                if result.get("id") == identifier:
                    if "error" in result:
                        # The isolated environment has no personal data or credentials.
                        raise RuntimeError(f"{method}: {result['error']}")
                    return result["result"]
            raise RuntimeError("Codex transport closed")
        try:
            call(1, "initialize", {"clientInfo": {"name": "forge_protocol_test", "version": "1"},
                                  "capabilities": {"experimentalApi": True}})
            assert call(2, "account/read", {"refreshToken": False})["account"] is None
            thread = call(3, "thread/start", {"cwd": str(workspace), "approvalPolicy": "never",
                "ephemeral": True, "dynamicTools": [{
                    "name": "app_forge_install_dependencies",
                    "description": "Reconcile dependencies from fixed workspace manifests.",
                    "inputSchema": {"type": "object", "properties": {},
                                    "additionalProperties": False}}]})
            turn = call(4, "turn/start", {"threadId": thread["thread"]["id"],
                "input": [{"type": "text", "text": "Offline protocol test. No application generation."}],
                "cwd": str(workspace), "approvalPolicy": "never"})
            assert turn["turn"]["id"]
            print("Codex: restricted turn and no-argument Dynamic Tool accepted; no account; offline")
        finally:
            process.terminate()
            process.wait(timeout=5)


if __name__ == "__main__":
    main()
