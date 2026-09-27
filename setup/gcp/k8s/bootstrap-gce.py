"""Run on the GCE K3s server: prepare runtime secrets, then initialize Cloud SQL.

Python standard library only. Credentials pass through memory/stdin, never argv.
This does not deploy the app, controllers, or public ingress.
"""
import argparse
import base64
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# プロジェクト名・イメージの置き場・ノード名は setup/environments/ が正。
# ここに書き写すと、テナントを移したときに直す場所が増える。
#
# ただしこの道具は、k8sフォルダだけをサーバVMへ転送して動かすこともある。
# その場合は環境ファイルが手元に無い。読めないことを失敗にはせず、既定値が
# 空になるだけにして、引数で渡してもらう。
_LOADER = Path(__file__).resolve().parents[2] / "environments" / "load.py"
if _LOADER.is_file():
    _spec = importlib.util.spec_from_file_location("forge_environments", _LOADER)
    environments = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(environments)
else:
    environments = None


def environment_defaults(argv=None):
    """--environment だけ先に読む。他の引数の既定値がこの選択で決まるため。

    この道具はGCE上のK3sサーバでだけ動かす。手元のk3sはこの経路を使わないので、
    既定は prod。
    """
    name = os.environ.get("ENV", "prod")
    if environments is None:
        return name, {}
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--environment", default=name)
    known, _ = pre.parse_known_args(argv)
    if known.environment not in environments.names():
        raise SystemExit(f"環境 {known.environment} がありません。あるのは: {', '.join(environments.names())}")
    return known.environment, environments.values(known.environment)


def add_environment_option(parser, environment):
    """環境ファイルが無いときは選択肢を出さない。選べないものを見せない。"""
    if environments is None:
        parser.set_defaults(environment=environment)
        return
    parser.add_argument("--environment", default=environment, choices=environments.names())


def require(parser, args, *names):
    """空のまま配備させない。空だと名前の無いホストや置き場を指しに行く。"""
    for name in names:
        if not getattr(args, name, ""):
            parser.error(f"--{name.replace('_', '-')} が空です。"
                         f"setup/environments/{args.environment}.env を埋めるか、引数で渡してください。")



KUBE = ["k3s", "kubectl", "--kubeconfig", "/etc/rancher/k3s/k3s.yaml",
        "--context", "default", "--request-timeout=30s"]


class SetupError(Exception):
    """Only deliberately credential-free operator messages belong here."""


def run(command, payload=None):
    result = subprocess.run(command, input=payload, text=True, capture_output=True)
    if result.returncode:
        # Both CLI errors and application exceptions can include credentials.
        raise SetupError(f"{command[0]} operation failed; credential-bearing output withheld")
    return result.stdout


def kube(*args, document=None):
    output = run([*KUBE, *args], json.dumps(document) if document is not None else None)
    return json.loads(output) if output.strip().startswith("{") else output


def get(kind, name, namespace="koyorina"):
    return kube("-n", namespace, "get", kind, name, "--ignore-not-found", "-o", "json") or None


TAG = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
# values.yamlのキー → Artifact Registry上のイメージ名（リリース名の後ろに付く部分）。
IMAGES = {"app": "", "agent": "-agent", "previewRuntime": "-preview-runtime"}


def top_level(text, key):
    """values.yamlの最上位のスカラー1つだけを読む。PyYAMLに頼らない（標準ライブラリのみ）。"""
    found = re.search(rf"^{key}:[ \t]*[\"']?([^\"'#\s]*)[\"']?[ \t]*(?:#.*)?$", text, re.M)
    return found.group(1) if found else ""


def resolve_image_tag(release, values_file):
    """imageTag（例: v0.1.0-dev）を、配備の瞬間に3イメージのdigestへ解決する。

    生成・プレビューのcontrollerは、digestで固定されたイメージしか起動しない
    （タグは後から別の中身へ付け替えられるため。worker/controller.py参照）。
    人が書くのはタグ、クラスタへ渡るのは常にdigest、という分担にする。
    同じタグで押し直した場合も、再配備すれば新しい中身に追従する。
    空なら何もせず、values.yamlのimages.*.digestをそのまま使う。
    """
    text = Path(values_file).read_text()
    tag = top_level(text, "imageTag")
    if not tag:
        return []
    registry = top_level(text, "registry")
    if not TAG.fullmatch(tag) or not registry:
        raise SetupError("imageTag or registry in the values file is invalid")
    overrides = []
    for key, suffix in IMAGES.items():
        reference = f"{registry}/{release}{suffix}:{tag}"
        try:
            digest = run(["gcloud", "artifacts", "docker", "images", "describe", reference,
                          "--format=value(image_summary.digest)"]).strip()
        except SetupError:
            raise SetupError(f"Image {reference} was not found; build and push it with this tag first")
        if not DIGEST.fullmatch(digest):
            raise SetupError(f"Image {reference} did not resolve to a digest")
        print(f"{reference} -> {digest}")
        overrides += ["--set", f"images.{key}.digest={digest}"]
    return overrides


def helm_upgrade(release, chart, namespace, values_file):
    """アプリ本体(app/codex-controller/preview)のワークロードだけをHelmへ渡す。
    Secret作成・DB初期化・pull tokenの更新はこの後も deploy-*-gce.py が担う
    （Chartはそれらを一切作らない。既存のSecretを名前で参照するだけ）。
    --create-namespaceは「Helmが所有していないnamespaceを作る」動作で、
    bootstrap-gce.py prepareが先に作るkoyorinaを壊さない。"""
    run(["helm", "upgrade", "--install", release, chart,
         "--namespace", namespace, "--create-namespace", "-f", values_file,
         *resolve_image_tag(release, values_file),
         "--kubeconfig", "/etc/rancher/k3s/k3s.yaml", "--kube-context", "default"])


def database_url(value):
    value = value.strip()
    parts = urlsplit(value)
    if (parts.scheme != "postgresql+psycopg" or not parts.hostname
            or not parts.username or not parts.password or parts.path in {"", "/"}
            or parts.fragment):
        raise SetupError("Secret Manager does not contain a valid PostgreSQL URL")
    query = dict(parse_qsl(parts.query))
    if query.get("sslmode", "require") not in {"require", "verify-ca", "verify-full"}:
        raise SetupError("Cloud SQL requires an encrypted connection")
    query.setdefault("sslmode", "require")
    return urlunsplit(parts._replace(query=urlencode(query)))


def runtime_document(existing, desired):
    """Fill missing keys, preserve credentials, reject conflicting environments."""
    previous = {key: base64.b64decode(value).decode()
                for key, value in (existing or {}).get("data", {}).items()}
    for key, value in desired.items():
        if key in previous and previous[key] != value:
            raise SetupError(f"Existing {key} differs; no secret was overwritten")
    previous.update(desired)
    previous.setdefault("APP_SESSION_SECRET", secrets.token_urlsafe(48))
    if len(previous["APP_SESSION_SECRET"]) < 32:
        raise SetupError("Existing session secret is too short; no secret was overwritten")
    document = json.loads(json.dumps(existing)) if existing else {
        "apiVersion": "v1", "kind": "Secret", "type": "Opaque",
        "metadata": {"name": "koyorina-runtime", "namespace": "koyorina"}}
    # Avoid apply's last-applied annotation containing another copy of credentials.
    document["metadata"].get("annotations", {}).pop("kubectl.kubernetes.io/last-applied-configuration", None)
    document["data"] = {key: base64.b64encode(value.encode()).decode()
                        for key, value in previous.items()}
    return document


def prepare(args):
    client_id = input("Google OAuth client ID: ").strip()
    admin_email = input("Initial admin email: ").strip().lower()
    origin = input("App HTTPS origin (https://your-domain): ").strip().rstrip("/")
    if not re.fullmatch(r"[0-9]+-[a-z0-9]+\.apps\.googleusercontent\.com", client_id):
        raise SetupError("Invalid Google OAuth client ID")
    if not re.fullmatch(r"[A-Za-z0-9._+%-]+@[A-Za-z0-9.-]+", admin_email):
        raise SetupError("Invalid admin email")
    parts = urlsplit(origin)
    if (parts.scheme != "https" or not parts.hostname or parts.username or parts.password
            or parts.path or parts.query or parts.fragment):
        raise SetupError("Specify an HTTPS origin without a path")
    url = database_url(run(["gcloud", "secrets", "versions", "access", "latest",
                           "--project", args.project, "--secret", args.database_secret]))
    existing = get("secret", f"{args.app_name}-runtime", args.app_name)
    document = runtime_document(existing, {
        "DATABASE_URL": url, "GOOGLE_OAUTH_CLIENT_ID": client_id,
        "BOOTSTRAP_ADMIN_EMAIL": admin_email, "APP_ORIGIN": origin,
        "APP_ENV": "production"})
    if not get("namespace", args.app_name):
        kube("create", "-f", "-", document={"apiVersion": "v1", "kind": "Namespace",
             "metadata": {"name": args.app_name}})
    document["metadata"]["name"] = f"{args.app_name}-runtime"
    document["metadata"]["namespace"] = args.app_name
    kube("replace" if existing else "create", "-f", "-", document=document)
    print("Runtime Secret prepared. Existing credentials were preserved.")


def job_document(image, node, name=None, only_migrate=False, app_name="koyorina"):
    # Do not copy the app Pod: bootstrap needs no controller tokens, SA or Vertex key.
    steps = ("['alembic', 'upgrade', 'head'], " if only_migrate
             else "['alembic', 'upgrade', 'head'], ['python', '-m', 'backend.bootstrap'], ")
    done = "Migration: OK" if only_migrate else "Cloud SQL migration and admin bootstrap: OK"
    name = name or f"{app_name}-gce-bootstrap-v1"
    naming = ({"generateName": name} if only_migrate else {"name": name})
    return {"apiVersion": "batch/v1", "kind": "Job",
        "metadata": {**naming, "namespace": app_name},
        "spec": {"backoffLimit": 0, "activeDeadlineSeconds": 600,
            "template": {"metadata": {"labels": {"app": "koyorina-bootstrap"}}, "spec": {
                "restartPolicy": "Never", "automountServiceAccountToken": False,
                "nodeSelector": {"kubernetes.io/hostname": node},
                 "imagePullSecrets": [{"name": f"{app_name}-bootstrap-pull"}],
                "securityContext": {"runAsNonRoot": True, "runAsUser": 10001,
                    "runAsGroup": 10001, "seccompProfile": {"type": "RuntimeDefault"}},
                "containers": [{"name": "bootstrap", "image": image, "imagePullPolicy": "Always",
                    "command": ["python", "-c"], "args": [
                        "import subprocess\n"
                        f"for command in ({steps}):\n"
                        "    result = subprocess.run(command, capture_output=True)\n"
                        "    if result.returncode:\n"
                        "        print('Bootstrap failed; credential-bearing output withheld', flush=True)\n"
                        "        raise SystemExit(result.returncode)\n"
                        f"print({done!r}, flush=True)\n"],
                     "envFrom": [{"secretRef": {"name": f"{app_name}-runtime"}}],
                    "securityContext": {"readOnlyRootFilesystem": True,
                        "allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}},
                    "resources": {"requests": {"cpu": "100m", "memory": "128Mi"},
                                  "limits": {"cpu": "1", "memory": "512Mi"}},
                    "volumeMounts": [{"name": "tmp", "mountPath": "/tmp"}]}],
                "volumes": [{"name": "tmp", "emptyDir": {"sizeLimit": "64Mi"}}]}}}}


def check_image(args, stage):
    """このJobは runtime Secret を丸ごと受け取る。DB接続URLもその中にある。

    つまり「どのイメージを動かすか」は、DBの中身を誰に見せるかと同じ重さがある。
    選んだプロジェクトのArtifact Registryのものだけを通す。
    """
    if not args.image or not args.server_node:
        raise SetupError(f"{stage} requires --image and --server-node")
    if not args.project:
        raise SetupError(f"{stage} requires --project (環境ファイルが無い場合は明示する)")
    if not re.fullmatch(r"[a-z0-9-]+-docker\.pkg\.dev/" + re.escape(args.project)
                        + r"/[a-z0-9._/-]+(?::[A-Za-z0-9._-]+|@sha256:[0-9a-f]{64})", args.image):
        raise SetupError("Specify an Artifact Registry image in the selected project, with tag or digest")


def bootstrap(args):
    check_image(args, "bootstrap")
    node = get("node", args.server_node)
    if (not node or "node-role.kubernetes.io/control-plane" not in node["metadata"].get("labels", {})
            or not any(c["type"] == "Ready" and c["status"] == "True"
                       for c in node.get("status", {}).get("conditions", []))):
        raise SetupError("The selected K3s server node is not Ready")
    runtime = get("secret", f"{args.app_name}-runtime", args.app_name)
    required = {"DATABASE_URL", "APP_SESSION_SECRET", "GOOGLE_OAUTH_CLIENT_ID",
                "BOOTSTRAP_ADMIN_EMAIL", "APP_ORIGIN", "APP_ENV"}
    if not runtime or not required <= runtime.get("data", {}).keys():
        raise SetupError("Run prepare first")
    previous = get("job", f"{args.app_name}-gce-bootstrap-v1", args.app_name)
    if previous:
        if previous.get("status", {}).get("succeeded"):
            print("Bootstrap already completed. No job was recreated.")
            return
        raise SetupError("Bootstrap Job already exists; inspect its state before retrying")
    # Short-lived VM identity, scoped to this bootstrap's image pull. Never a SA key.
    token = run(["gcloud", "auth", "print-access-token", "--project", args.project]).strip()
    if not token:
        raise SetupError("No access token was returned")
    host = args.image.split("/", 1)[0]
    auth = base64.b64encode(("oauth2accesstoken:" + token).encode()).decode()
    dockerconfig = json.dumps({"auths": {host: {"auth": auth}}})
    pull = get("secret", f"{args.app_name}-bootstrap-pull", args.app_name)
    pull_document = {"apiVersion": "v1", "kind": "Secret", "type": "kubernetes.io/dockerconfigjson",
        "metadata": {"name": f"{args.app_name}-bootstrap-pull", "namespace": args.app_name},
        "data": {".dockerconfigjson": base64.b64encode(dockerconfig.encode()).decode()}}
    if pull:
        pull_document["metadata"]["resourceVersion"] = pull["metadata"]["resourceVersion"]
    kube("replace" if pull else "create", "-f", "-", document=pull_document)
    hostname = node["metadata"]["labels"]["kubernetes.io/hostname"]
    kube("create", "-f", "-", document=job_document(args.image, hostname, app_name=args.app_name))
    print("Bootstrap Job created. Check completion with the commands in README.")


def migrate(args):
    """2回目以降のDB移行。bootstrapとは別のJobとして、何度でも流せる形にする。

    bootstrap の Job は `koyorina-gce-bootstrap-v1` という固定名で、完了済みなら
    作り直さない（初期管理者の登録を勝手に繰り返さないため）。そのままでは
    2回目の移行を当てる手段が無く、手でJobを消して作り直すことになっていた。

    こちらは generateName で毎回別の名前を作り、alembic だけを流す。
    未適用のものだけが順に当たり、適用済みは飛ばされる。
    """
    check_image(args, "migrate")
    runtime = get("secret", f"{args.app_name}-runtime", args.app_name)
    if not runtime or "DATABASE_URL" not in runtime.get("data", {}):
        raise SetupError("Run prepare first")
    node = get("node", args.server_node)
    if not node:
        raise SetupError("The selected K3s server node was not found")
    if not get("secret", f"{args.app_name}-pull", args.app_name):
        raise SetupError(f"{args.app_name}-pull is missing; deploy the management app first")
    hostname = node["metadata"]["labels"]["kubernetes.io/hostname"]
    document = job_document(args.image, hostname, f"{args.app_name}-gce-migrate-",
                            only_migrate=True, app_name=args.app_name)
    # 取得認証はアプリと同じものを使う。移行のためだけに短期トークンを作らない。
    document["spec"]["template"]["spec"]["imagePullSecrets"] = [{"name": f"{args.app_name}-pull"}]
    kube("create", "-f", "-", document=document)
    print("Migration Job created. Check its logs for 'Migration: OK'.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    environment, defaults = environment_defaults()
    parser.add_argument("stage", choices=["prepare", "bootstrap", "migrate"])
    add_environment_option(parser, environment)
    parser.add_argument("--project", default=defaults.get("GCP_PROJECT", ""))
    parser.add_argument("--database-secret", default="koyorina-database-url")
    parser.add_argument("--app-name", default=defaults.get("APP_NAME", "koyorina"))
    image_root = defaults.get("IMAGE_ROOT", "")
    app_name = defaults.get("APP_NAME", "koyorina")
    parser.add_argument("--image", default=f"{image_root}/{app_name}:latest"
                        if image_root and app_name else None)
    parser.add_argument("--server-node", default=defaults.get("SERVER_NODE") or None)
    args = parser.parse_args()
    try:
        {"prepare": prepare, "bootstrap": bootstrap, "migrate": migrate}[args.stage](args)
    except SetupError as error:
        raise SystemExit(str(error)) from None
    except (RuntimeError, ValueError, OSError):
        # Exception text may contain a connection URL or token: do not echo it.
        raise SystemExit("Setup failed. Check inputs, existing resource state and VM IAM permissions. "
                         "No credential-bearing output is shown.") from None


if __name__ == "__main__":
    main()
