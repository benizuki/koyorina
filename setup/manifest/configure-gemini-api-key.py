#!/usr/bin/env python3
"""Google AI StudioのAPIキーを、注釈へ残さず両namespaceのSecretへ保存する。"""
import argparse
import importlib.util
import json
import os
import subprocess
from pathlib import Path

# ドメインやイメージの置き場と同じく、アプリ名も setup/environments/ が正。
_spec = importlib.util.spec_from_file_location(
    "forge_environments", Path(__file__).resolve().parents[1] / "environments" / "load.py")
environments = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(environments)


def kubectl(context: str, args: list[str], document=None, *, optional=False):
    command = ["kubectl", "--context", context, *args]
    result = subprocess.run(command, input=json.dumps(document) if document else None,
                            text=True, capture_output=True)
    if result.returncode:
        if optional and "NotFound" in result.stderr:
            return None
        raise SystemExit("Kubernetes Secretの設定に失敗しました。contextと権限を確認してください。")
    return json.loads(result.stdout) if result.stdout.strip().startswith("{") else None


def store(context: str, namespace: str, name: str, api_key: str):
    existing = kubectl(context, ["-n", namespace, "get", "secret", name, "-o", "json"],
                       optional=True)
    document = {"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
                "metadata": {"name": name, "namespace": namespace},
                "stringData": {"GEMINI_API_KEY": api_key}}
    if existing:
        document["metadata"]["resourceVersion"] = existing["metadata"]["resourceVersion"]
        kubectl(context, ["replace", "-f", "-"], document)
    else:
        kubectl(context, ["create", "-f", "-"], document)


def main():
    parser = argparse.ArgumentParser(description="Gemini Developer APIキーをKubernetes Secretへ保存します。")
    parser.add_argument("--context", default=os.getenv("KUBE_CONTEXT", "default"))
    parser.add_argument("--environment", default=os.environ.get("ENV", environments.DEFAULT),
                        choices=environments.names())
    args = parser.parse_args()
    api_key = os.getenv("GEMINI_API_KEY", "")
    if not api_key:
        parser.error("GEMINI_API_KEYを環境変数へ設定してください。")
    app_name = environments.required(args.environment, "APP_NAME")
    namespaces = (app_name, f"{app_name}-codex")
    secret_name = f"{app_name}-gemini-api"
    for namespace in namespaces:
        store(args.context, namespace, secret_name, api_key)
    print(f"Gemini Developer APIキーを{namespaces[0]}と{namespaces[1]}へ設定しました。")


if __name__ == "__main__":
    main()
