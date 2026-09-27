"""Operator-only initial provisioning. Secrets stay in memory/stdin, never argv/output.

Run prepare, then bootstrap after PostgreSQL is Ready. Does not publish a Gateway.
Existing secrets are never overwritten. No generated code may invoke this script.
"""
import argparse
import base64
import copy
import importlib.util
import json
import os
from pathlib import Path
import secrets
import subprocess
import re
import yaml

ROOT = Path(__file__).parent

# APP_NAME・DOMAINなど環境ごとの値は setup/environments/ が正。ここには持たせない。
_spec = importlib.util.spec_from_file_location(
    "forge_environments", ROOT.parent / "environments" / "load.py")
environments = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(environments)


def kube(context, namespace, *args, payload=None):
    result = subprocess.run(
        ["kubectl", "--context", context, "--namespace", namespace, "--request-timeout=30s", *args],
        input=payload, capture_output=True, text=True)
    if result.returncode:
        # kubectl/psql can reflect the submitted manifest or SQL on failure.
        raise SystemExit("操作が失敗しました（秘密情報保護のため出力省略）: " + args[0])
    return result.stdout


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=["prepare", "bootstrap"])
    # Ansibleからはプロセス一覧へ資格情報を出さないよう環境変数で渡す。
    # 手動実行との互換性のため、従来のコマンドライン引数も利用できる。
    parser.add_argument("--client-id", default=os.environ.get("GOOGLE_OAUTH_CLIENT_ID"))
    parser.add_argument("--admin-email", default=os.environ.get("BOOTSTRAP_ADMIN_EMAIL"))
    parser.add_argument("--environment", default=os.environ.get("ENV", environments.DEFAULT),
                        choices=environments.names())
    args = parser.parse_args()
    if not args.client_id:
        raise SystemExit("OAuthクライアントIDが指定されていません。")
    if not args.admin_email:
        raise SystemExit("初期管理者メールアドレスが指定されていません。")
    if not re.fullmatch(r"[0-9]+-[a-z0-9]+\.apps\.googleusercontent\.com", args.client_id):
        raise SystemExit("OAuthクライアントIDの形式が不正です。")
    if not re.fullmatch(r"[A-Za-z0-9._+%-]+@[A-Za-z0-9.-]+", args.admin_email):
        raise SystemExit("管理者メールの形式が不正です。")

    app_name = environments.required(args.environment, "APP_NAME")
    context = os.environ.get("KUBE_CONTEXT", "default")

    def kubectl(*cmd_args, payload=None):
        return kube(context, app_name, *cmd_args, payload=payload)

    def create(obj):
        kubectl("create", "-f", "-", payload=json.dumps(obj))

    def read_secret(name):
        value = kubectl("get", "secret", name, "--ignore-not-found", "-o", "json")
        if not value.strip():
            return None
        return {key: base64.b64decode(val).decode() for key, val in json.loads(value)["data"].items()}

    def ensure_secret(name, values):
        previous = read_secret(name)
        if previous is not None:
            return previous
        create({"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
                "metadata": {"name": name, "namespace": app_name}, "stringData": values})
        return values

    def manifest(name):
        return environments.render((ROOT / name).read_text(), args.environment)

    if args.stage == "prepare":
        kubectl("apply", "-f", "-", payload=manifest("namespace.yaml"))
        ensure_secret("postgres-admin", {"password": secrets.token_urlsafe(48)})
        credential = ensure_secret("forge-db-login", {"password": secrets.token_urlsafe(48)})
        runtime = ensure_secret(f"{app_name}-runtime", {
            "DATABASE_URL": "postgresql+psycopg://forge:" + credential["password"] + "@postgres:5432/forge",
            "APP_SESSION_SECRET": secrets.token_urlsafe(48),
            "GOOGLE_OAUTH_CLIENT_ID": args.client_id,
            "BOOTSTRAP_ADMIN_EMAIL": args.admin_email.lower(),
        })
        if runtime["GOOGLE_OAUTH_CLIENT_ID"] != args.client_id or runtime["BOOTSTRAP_ADMIN_EMAIL"] != args.admin_email.lower():
            raise SystemExit("既存の認証設定と不一致のため停止しました。既存Secretは変更していません。")
        kubectl("apply", "-f", "-", payload=manifest("postgres.yaml"))
        print("専用namespace・Secret・PostgreSQLを準備しました。", flush=True)
        return

    password = read_secret("forge-db-login")["password"]
    if not re.fullmatch(r"[A-Za-z0-9_-]+", password):
        raise SystemExit("想定外の資格情報形式です。")
    # SQL and password are stdin only. Never reset an existing role password/database.
    sql = rf"""
DO $$ BEGIN
IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='forge') THEN
CREATE ROLE forge LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD '{password}';
END IF;
END $$;
SELECT 'CREATE DATABASE forge OWNER forge' WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname='forge')
\gexec
REVOKE ALL ON DATABASE forge FROM PUBLIC;
\connect forge
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
"""
    kubectl("exec", "-i", "postgres-0", "--", "psql", "-U", "postgres", "-v", "ON_ERROR_STOP=1", payload=sql)
    documents = [doc for doc in yaml.safe_load_all(manifest("app.yaml")) if doc]
    config = next(doc for doc in documents if doc["kind"] == "ConfigMap")
    deploy = next(doc for doc in documents if doc["kind"] == "Deployment")
    kubectl("apply", "-f", "-", payload=json.dumps(config))
    bootstrap_job = f"{app_name}-bootstrap-v1"
    existing_job = kubectl("get", "job", bootstrap_job, "--ignore-not-found", "-o", "json")
    if existing_job.strip():
        status = json.loads(existing_job).get("status", {})
        if status.get("succeeded", 0):
            print("既存の初期化Jobは完了しています。", flush=True)
            return
        if status.get("failed", 0):
            # イメージ取得失敗などで終了したJobは同名で作り直せないため、
            # Jobだけを削除して再作成する。DBとPVCは削除しない。
            kubectl("delete", "job", bootstrap_job, "--wait=true", "--timeout=60s")
        else:
            print("既存の初期化Jobが実行中です。完了を待ちます。", flush=True)
            return
    spec = copy.deepcopy(deploy["spec"]["template"]["spec"])
    spec["restartPolicy"] = "Never"
    # 読み取り用ServiceAccountも通常アプリと同時に作られるため、初期化Jobでは
    # 参照しない。JobはKubernetes APIを呼ばないのでトークンも不要。
    spec.pop("serviceAccountName", None)
    spec["automountServiceAccountToken"] = False
    container = spec["containers"][0]
    for key in ("readinessProbe", "livenessProbe", "startupProbe", "ports"):
        container.pop(key, None)
    # DBマイグレーションと初期管理者登録には、管理アプリからDB・OAuth設定を
    # 渡すenvFromだけあればよい。通常アプリのenvを引き継ぐと、まだ作成前の
    # Codex/Preview/Gemini Secretまで起動条件になり、初回配備が循環して止まる。
    container.pop("env", None)
    container["command"] = ["/bin/sh", "-ec", "alembic upgrade head && python -m backend.bootstrap"]
    create({"apiVersion": "batch/v1", "kind": "Job",
            "metadata": {"name": bootstrap_job, "namespace": app_name},
            "spec": {"backoffLimit": 0, "activeDeadlineSeconds": 300,
                     "template": {"metadata": {"labels": {"app": app_name}}, "spec": spec}}})
    print("専用DBを準備し、マイグレーション・初期管理者登録Jobを作成しました。", flush=True)


if __name__ == "__main__":
    main()
