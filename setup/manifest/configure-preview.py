"""Operator-only: configure the preview controller and its shared token."""

import argparse
import base64
import importlib.util
import json
import os
from pathlib import Path
import secrets
import subprocess

import yaml


ROOT = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location(
    "forge_environments", ROOT.parent / "environments" / "load.py")
environments = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(environments)

CONTEXT = os.environ.get("KUBE_CONTEXT", "default")


def kube(*args, payload=None, optional=False):
    result = subprocess.run(
        ["kubectl", "--context", CONTEXT, *args],
        input=json.dumps(payload) if payload is not None else None,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        if optional and "NotFound" in result.stderr:
            return None
        raise SystemExit("Kubernetes操作に失敗しました。認証・対象リソースを確認してください。")
    return json.loads(result.stdout) if result.stdout.strip().startswith("{") else None


def read_token(namespace, name, key):
    value = kube("-n", namespace, "get", "secret", name, "-o", "json", optional=True)
    if not value:
        return None
    try:
        return base64.b64decode(value["data"][key]).decode()
    except (KeyError, ValueError):
        raise SystemExit(f"既存Secret {namespace}/{name} の形式が不正です。") from None


def create_secret(namespace, name, key, token):
    kube("-n", namespace, "create", "-f", "-", payload={
        "apiVersion": "v1",
        "kind": "Secret",
        "type": "Opaque",
        "metadata": {"name": name, "namespace": namespace},
        "stringData": {key: token},
    })


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--environment",
        default=os.environ.get("ENV", environments.DEFAULT),
        choices=environments.names(),
    )
    args = parser.parse_args()
    app_name = environments.required(args.environment, "APP_NAME")
    preview_namespace = f"{app_name}-preview"
    controller_name = f"{app_name}-preview-controller"
    client_name = f"{app_name}-preview-client"

    documents = [document for document in yaml.safe_load_all(environments.render(
        (ROOT / "preview.yaml").read_text(), args.environment)) if document]
    namespace = next(document for document in documents if document["kind"] == "Namespace")
    kube("apply", "-f", "-", payload=namespace)

    controller_token = read_token(preview_namespace, controller_name, "token")
    client_token = read_token(app_name, client_name, "PREVIEW_CONTROLLER_TOKEN")
    if controller_token and client_token and controller_token != client_token:
        raise SystemExit("既存のPreviewサービス間鍵が一致しません。上書きせず停止しました。")
    token = controller_token or client_token or secrets.token_urlsafe(48)
    if len(token) < 32:
        raise SystemExit("既存のPreviewサービス間鍵が短すぎるため停止しました。")
    if not controller_token:
        create_secret(preview_namespace, controller_name, "token", token)
    if not client_token:
        create_secret(app_name, client_name, "PREVIEW_CONTROLLER_TOKEN", token)

    # テナントに登録する秘密（生成アプリ用のGemini APIキー）を暗号化する鍵。
    # 無ければ作る。既にあれば決して書き換えない（変えると保存済みの値を開けなくなる）。
    tenant_secrets = f"{app_name}-tenant-secrets"
    if not read_token(app_name, tenant_secrets, "TENANT_SECRET_KEY"):
        create_secret(app_name, tenant_secrets, "TENANT_SECRET_KEY", secrets.token_urlsafe(48))

    for document in documents:
        if document["kind"] != "Namespace":
            kube("apply", "-f", "-", payload=document)
    print("Preview Controllerの設定を反映しました。")


if __name__ == "__main__":
    main()
