"""Deploy the management app after Cloud SQL bootstrap. Run as root on the K3s server.

ワークロード本体(Deployment/Service/RBAC/NetworkPolicy等)はsetup/helm/koyorinaの
Helm Chartが持つ。ここに残るのは、Chartが作らない・作ってはいけないもの
（Secretのガード付き作成、Artifact Registryのpull tokenの定期更新、
Cloud SQL初期化の前提確認）だけ。生マニフェストの組み立てはもう行わない。
"""
import base64
import argparse
import importlib.util
import json
from pathlib import Path
import re
import shutil
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent
module_spec = importlib.util.spec_from_file_location("bootstrap_gce", ROOT / "bootstrap-gce.py")
common = importlib.util.module_from_spec(module_spec)
module_spec.loader.exec_module(common)
kube, get, run, SetupError = common.kube, common.get, common.run, common.SetupError
helm_upgrade = common.helm_upgrade
# 環境ファイルの読み口は bootstrap-gce.py が持つ。読む場所を2つにしない。
environments = common.environments
environment_defaults, add_environment_option = common.environment_defaults, common.add_environment_option
require = common.require
MARKER = "# Managed by Koyorina GCE management deployment"


def decode(document):
    return {key: base64.b64decode(value).decode() for key, value in document.get("data", {}).items()}


def upsert_secret(name, namespace, values, secret_type="Opaque", *, preserve=True):
    previous = get("secret", name, namespace)
    data = decode(previous) if previous else {}
    for key, value in values.items():
        if preserve and key in data and data[key] != value:
            raise SetupError("Existing service credential differs; no overwrite performed")
        data[key] = value
    document = {"apiVersion": "v1", "kind": "Secret", "type": secret_type,
                "metadata": {"name": name, "namespace": namespace},
                "data": {key: base64.b64encode(value.encode()).decode() for key, value in data.items()}}
    if previous:
        document["metadata"] = dict(previous["metadata"])
        document["metadata"].get("annotations", {}).pop("kubectl.kubernetes.io/last-applied-configuration", None)
    kube("replace" if previous else "create", "-f", "-", document=document)


def refresh_pull(args):
    token = run(["gcloud", "auth", "print-access-token", "--project", args.project]).strip()
    if not token:
        raise SetupError("VM access token was not returned")
    auth = base64.b64encode(("oauth2accesstoken:" + token).encode()).decode()
    config = json.dumps({"auths": {args.registry_host: {"auth": auth}}})
    upsert_secret(f"{args.release}-pull", args.release, {".dockerconfigjson": config},
                  "kubernetes.io/dockerconfigjson", preserve=False)
    print("Artifact Registry pull credential refreshed.")


def ensure_tenant_secret_key(args):
    """DBに保存するAPIキーを暗号化する鍵(TENANT_SECRET_KEY)を、Secret Managerを正として揃える。

    鍵がクラスタの中にしか無いと、クラスタを作り直した時点で新しい鍵が作られ、
    Cloud SQLに残っている暗号化済みのAPIキーが全部開けなくなる。DBと同じく
    クラスタの外(Secret Manager)に置き、k8sのSecretはそこからの写しにする。

    - Secret Managerにある → k8sへ写す（作り直し後の復元）
    - Secret Managerに無く、k8sにだけある → その値をSecret Managerへ上げる（既存環境の移行）
    - どちらにも無い → 生成してSecret Managerへ先に入れ、それからk8sへ
    - 両方にあって食い違う → 止める。どちらが正しいか機械的には決められず、
      間違えると保存済みの値を失う。値は標準入力で渡し、argvやログへ出さない。
    """
    name, namespace = f"{args.release}-tenant-secrets", args.release
    secret = ["--project", args.project, "--secret", args.tenant_key_secret]
    # 版が無いこととアクセスできないことを区別する。accessは両方とも失敗になる。
    versions = run(["gcloud", "secrets", "versions", "list", "--project", args.project,
                    args.tenant_key_secret, "--filter=state:ENABLED", "--limit=1",
                    "--format=value(name)"]).strip()
    stored = run(["gcloud", "secrets", "versions", "access", "latest", *secret]) if versions else ""
    current = get("secret", name, namespace)
    in_cluster = decode(current).get("TENANT_SECRET_KEY", "") if current else ""
    if stored and in_cluster and stored != in_cluster:
        raise SetupError(f"TENANT_SECRET_KEY in the cluster differs from Secret Manager "
                         f"({args.tenant_key_secret}); no overwrite performed")
    key = stored or in_cluster or common.secrets.token_urlsafe(48)
    if len(key) < 32:
        raise SetupError("TENANT_SECRET_KEY is too short")
    if not stored:
        run(["gcloud", "secrets", "versions", "add", args.tenant_key_secret, "--project", args.project,
             "--data-file=-"], key)
        print(f"TENANT_SECRET_KEY saved to Secret Manager ({args.tenant_key_secret}).")
    if not in_cluster:
        upsert_secret(name, namespace, {"TENANT_SECRET_KEY": key})
        print("TENANT_SECRET_KEY restored into the cluster from Secret Manager."
              if stored else "TENANT_SECRET_KEY created.")


def ensure_pair(namespace, controller_name, client_name, client_key, client_namespace="koyorina"):
    controller = get("secret", controller_name, namespace)
    client = get("secret", client_name, client_namespace)
    controller_token = decode(controller).get("token") if controller else None
    client_token = decode(client).get(client_key) if client else None
    if controller_token and client_token and controller_token != client_token:
        raise SetupError("Controller and management credentials differ")
    token = controller_token or client_token or common.secrets.token_urlsafe(48)
    if len(token) < 32:
        raise SetupError("Existing controller token is too short")
    upsert_secret(controller_name, namespace, {"token": token})
    upsert_secret(client_name, client_namespace, {client_key: token})


def install_refresh_timer(args):
    target = Path("/opt/koyorina-gce-management")
    target.mkdir(mode=0o755, parents=True, exist_ok=True)
    for name in ("deploy-app-gce.py", "bootstrap-gce.py"):
        source = ROOT / name
        destination = target / name
        if source.resolve() != destination.resolve():
            shutil.copyfile(source, destination)
        destination.chmod(0o644)
    service = MARKER + "\n" + """[Unit]
Description=Refresh Koyorina Artifact Registry pull credential
After=network-online.target k3s.service
Wants=network-online.target

[Service]
Type=oneshot
User=root
Environment=PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/snap/bin
""" + f"ExecStart=/usr/bin/python3 {target}/deploy-app-gce.py refresh-pull --project {args.project} --registry-host {args.registry_host} --release {args.release}\n"
    timer = MARKER + "\n" + f"""[Unit]
Description=Refresh Koyorina image pull credential every 20 minutes

[Timer]
OnBootSec=30s
OnUnitActiveSec=20min
Unit={args.release}-pull-refresh.service

[Install]
WantedBy=timers.target
"""
    for name, content in ((f"{args.release}-pull-refresh.service", service),
                          (f"{args.release}-pull-refresh.timer", timer)):
        destination = Path("/etc/systemd/system") / name
        if destination.exists() and not destination.read_text().startswith(MARKER):
            raise SetupError("An unmanaged pull refresh unit already exists")
        destination.write_text(content)
    run(["systemctl", "daemon-reload"])
    run(["systemctl", "enable", "--now", f"{args.release}-pull-refresh.timer"])
    run(["systemctl", "start", f"{args.release}-pull-refresh.service"])


def deploy(args):
    runtime = get("secret", f"{args.release}-runtime", args.release)
    values = decode(runtime) if runtime else {}
    required = {"DATABASE_URL", "APP_SESSION_SECRET", "GOOGLE_OAUTH_CLIENT_ID",
                "BOOTSTRAP_ADMIN_EMAIL", "APP_ORIGIN", "APP_ENV"}
    if not required <= values.keys() or values["APP_ENV"] != "production":
        raise SetupError("Complete the GCE bootstrap prepare stage first")
    origin = urlsplit(values["APP_ORIGIN"])
    if origin.scheme != "https" or not origin.hostname:
        raise SetupError("Runtime APP_ORIGIN must be HTTPS")
    job = get("job", f"{args.release}-gce-bootstrap-v1", args.release)
    if not job or not job.get("status", {}).get("succeeded"):
        raise SetupError("Cloud SQL bootstrap has not completed")
    node = get("node", args.server_node)
    if (not node or "node-role.kubernetes.io/control-plane" not in node["metadata"].get("labels", {})
            or not any(c["type"] == "Ready" and c["status"] == "True"
                       for c in node.get("status", {}).get("conditions", []))):
        raise SetupError("The selected K3s server is not Ready")
    if (getattr(args, "gemini_api_backend", "vertex") == "developer"
            and not get("secret", f"{args.release}-gemini-api", args.release)):
        raise SetupError(f"Create {args.release}-gemini-api Secret before using the Developer API")
    # koyorina-codex/koyorina-preview namespaceはここでは作らない。Helm Chartの
    # テンプレート(codex-controller-namespace.yaml/preview-namespace.yaml)が
    # 所有権つきで作る前提で、先にここで作ってしまうと「Helmが管理していない
    # 既存リソース」としてhelm upgradeが拒否する。
    helm_upgrade(args.release, args.chart, args.release, args.values)
    # namespace自体はChartが作った直後なので、この時点でensure_pairが通る。
    ensure_pair(f"{args.release}-codex", f"{args.release}-codex-controller",
                f"{args.release}-codex-client", "CODEX_CONTROLLER_TOKEN", args.release)
    ensure_pair(f"{args.release}-preview", f"{args.release}-preview-controller",
                f"{args.release}-preview-client", "PREVIEW_CONTROLLER_TOKEN", args.release)
    ensure_tenant_secret_key(args)
    refresh_pull(args)
    install_refresh_timer(args)
    print("Management app applied via Helm. Waiting for readiness...", flush=True)
    # The request timeout must exceed the rollout watch timeout.
    command = [*common.KUBE[:-1], "--request-timeout=660s", "-n", args.release,
               "rollout", "status", f"deployment/{args.release}", "--timeout=600s"]
    run(command)
    print("Management app Ready. Generation/preview controllers become Ready once their own deploy stage runs.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    environment, defaults = environment_defaults()
    image_root = defaults.get("IMAGE_ROOT", "")
    parser.add_argument("stage", choices=["deploy", "refresh-pull"])
    add_environment_option(parser, environment)
    parser.add_argument("--project", default=defaults.get("GCP_PROJECT", ""))
    parser.add_argument("--gemini-api-backend", choices=("vertex", "developer"),
                        default=defaults.get("GEMINI_API_BACKEND", "vertex"))
    parser.add_argument("--registry-host", default=image_root.split("/")[0])
    parser.add_argument("--server-node", default=defaults.get("SERVER_NODE", ""))
    # Helm Chart自体・イメージdigestを含むvalues.yamlは、group_vars/pin.py相当の
    # 手作業更新に任せる（make pin相当のHelm版は別途）。ここでは場所を指すだけ。
    parser.add_argument("--chart", default="/opt/koyorina-setup/helm/koyorina")
    parser.add_argument("--values", default="/opt/koyorina-setup/helm/koyorina-values.gce.yaml")
    parser.add_argument("--release", default=defaults.get("APP_NAME", "koyorina"))
    # terraform(secrets.tf)が作る置き場。値はこのスクリプトが入れる（tfstateへ載せない）。
    parser.add_argument("--tenant-key-secret", default="")
    args = parser.parse_args()
    args.tenant_key_secret = args.tenant_key_secret or f"{args.release}-tenant-secret-key"
    needed = {"deploy": ("project", "registry_host", "server_node", "chart", "values"),
              # refresh-pull は取得トークンを入れ替えるだけ。
              "refresh-pull": ("project", "registry_host")}[args.stage]
    require(parser, args, *needed)
    if not re.fullmatch(r"[a-z][a-z0-9-]{4,62}", args.project):
        parser.error("Invalid project ID")
    if not re.fullmatch(r"[a-z0-9-]+-docker\.pkg\.dev", args.registry_host):
        parser.error("Invalid Artifact Registry hostname")
    if args.stage == "deploy" and not Path(args.values).is_file():
        parser.error(f"values file not found: {args.values}")
    try:
        (deploy if args.stage == "deploy" else refresh_pull)(args)
    except SetupError as error:
        raise SystemExit(str(error)) from None
    except (ValueError, OSError, KeyError):
        raise SystemExit("Deployment failed; credential-bearing output withheld. Check inputs and VM permissions.") from None


if __name__ == "__main__":
    main()
