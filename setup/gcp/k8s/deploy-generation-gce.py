"""Deploy generation on the K3s server. No private keys or credential-bearing logs.

生成controllerのワークロード本体はsetup/helm/koyorinaのHelm Chartが持つ
（deploy-app-gce.pyが先に helm upgrade --install 済みという前提）。
ここに残るのは、Chartが作らないpull tokenの定期更新と、
ローカルストレージ(local-path)のノード紐付けだけ。

生成エージェントの Vertex AI 認証は、画面（システム設定 → Gemini）の Workload Identity 連携で行う。
以前の「VMのタイマーが短命トークンを更新して渡す」方式（token-file）は廃止した。
生成に使う Antigravity SDK がその渡し方を読めないため。
"""
import argparse
import base64
import importlib.util
import json
import re
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("management", ROOT / "deploy-app-gce.py")
management = importlib.util.module_from_spec(spec)
spec.loader.exec_module(management)
common = management.common
run, kube, get, SetupError = common.run, common.kube, common.get, common.SetupError
helm_upgrade = common.helm_upgrade
NS = "koyorina-codex"
CONTROLLER = "koyorina-codex-controller"
CLIENT = "koyorina-codex-client"
PULL_SECRET = "koyorina-generation-pull"
AGENT_TAINT = "koyorina-agent"
MARKER = "# Managed by Koyorina generation deployment"


def configure_names(release):
    global NS, CONTROLLER, CLIENT, PULL_SECRET, AGENT_TAINT
    NS = f"{release}-codex"
    CONTROLLER = f"{release}-codex-controller"
    CLIENT = f"{release}-codex-client"
    PULL_SECRET = f"{release}-generation-pull"
    AGENT_TAINT = f"{release}-agent"


def refresh(args):
    # Running under root on the server VM uses the node identity, only in this process.
    token = run(["gcloud", "auth", "print-access-token", "--project", args.project]).strip()
    if not token:
        raise SetupError("VM access token not available")
    auth = base64.b64encode(("oauth2accesstoken:" + token).encode()).decode()
    config = json.dumps({"auths": {args.registry_host: {"auth": auth}}})
    management.upsert_secret(PULL_SECRET, NS,
                             {".dockerconfigjson": config},
                             "kubernetes.io/dockerconfigjson", preserve=False)
    print("Generation credentials refreshed.")


def configure_storage(hostname):
    config = get("configmap", "local-path-config", "kube-system")
    if not config:
        raise SetupError("local-path provisioner configuration not found")
    data = json.loads(config["data"]["config.json"])
    mappings = data.get("nodePathMap", [])
    existing = next((item for item in mappings if item["node"] == hostname), None)
    path = "/srv/forge/generation"
    if existing and existing["paths"] != [path]:
        raise SetupError("Existing node storage paths differ; review them before deployment")
    if not existing:
        mappings.append({"node": hostname, "paths": [path]})
    data["nodePathMap"] = mappings
    # Let kubectl parse the existing helper Pod YAML, preserving its other fields.
    helper = json.loads(run([*common.KUBE, "create", "--dry-run=client", "--validate=false",
                             "-f", "-", "-o", "json"], config["data"]["helperPod.yaml"]))
    toleration = {"key": "workload", "operator": "Equal",
                  "value": AGENT_TAINT, "effect": "NoSchedule"}
    tolerations = helper["spec"].setdefault("tolerations", [])
    if toleration not in tolerations:
        tolerations.append(toleration)
    config["data"]["config.json"] = json.dumps(data)
    config["data"]["helperPod.yaml"] = json.dumps(helper)
    kube("replace", "-f", "-", document=config)


def install_timer(args):
    target = Path("/opt/koyorina-gce-generation")
    target.mkdir(mode=0o755, parents=True, exist_ok=True)
    for name in ("deploy-generation-gce.py", "deploy-app-gce.py", "bootstrap-gce.py"):
        destination = target / name
        if (ROOT / name).resolve() != destination.resolve():
            shutil.copyfile(ROOT / name, destination)
        destination.chmod(0o644)
    service = MARKER + "\n" + """[Unit]
Description=Refresh Koyorina generation credentials
After=network-online.target k3s.service
Wants=network-online.target
[Service]
Type=oneshot
User=root
Environment=PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/snap/bin
""" + (f"ExecStart=/usr/bin/python3 {target}/deploy-generation-gce.py refresh "
         f"--project {args.project} --image-root {args.image_root} "
         f"--release {args.release}\n")
    timer = MARKER + "\n" + f"""[Unit]
Description=Refresh generation credentials every 20 minutes
[Timer]
OnBootSec=30s
OnUnitActiveSec=20min
Unit={args.release}-generation-refresh.service
[Install]
WantedBy=timers.target
"""
    for name, content in ((f"{args.release}-generation-refresh.service", service),
                          (f"{args.release}-generation-refresh.timer", timer)):
        path = Path("/etc/systemd/system") / name
        if path.exists() and not path.read_text().startswith(MARKER):
            raise SetupError("Unmanaged generation refresh unit exists")
        path.write_text(content)
    run(["systemctl", "daemon-reload"])
    run(["systemctl", "enable", "--now", f"{args.release}-generation-refresh.timer"])


def deploy(args):
    # RWX(Filestore/NFS)ではノード固定の前提そのものが無い。local-path固有の
    # 検査（--storage-verified、workspacesラベル、local-path-configのパッチ）は
    # 丸ごと飛ばす。
    rwx = getattr(args, "storage_access_mode", "ReadWriteOnce") == "ReadWriteMany"
    if not rwx and not args.storage_verified:
        raise SetupError("Verify /srv/forge is mounted on the agent, then pass --storage-verified")
    # RWXでは特定のagentへ固定しない（ansible.env.j2もAGENT_NODEを空にする）ので、
    # agent固有の確認はRWOのときだけ行う。
    for node_name in (args.server_node, *([] if rwx else [args.agent_node])):
        node = get("node", node_name)
        if not node or not any(c["type"] == "Ready" and c["status"] == "True"
                               for c in node.get("status", {}).get("conditions", [])):
            raise SetupError("Selected node is not Ready")
    if not rwx:
        labels = node["metadata"].get("labels", {})
        if labels.get("storage") != "workspaces":
            raise SetupError("Agent does not have the persistent workspace disk label")
        hostname = labels["kubernetes.io/hostname"]
    if not get("secret", CONTROLLER, NS) or not get("secret", CLIENT, args.release):
        raise SetupError("Deploy the management application first")
    # Keep the existing management/controller token pair and detect a mismatch.
    # namespace koyorina-codexは deploy-app-gce.py の helm upgrade で作られている前提
    # （このスクリプトを単独で再実行してもよいよう、helm upgradeはここでも冪等に呼ぶ）。
    management.ensure_pair(NS, CONTROLLER, CLIENT, "CODEX_CONTROLLER_TOKEN", args.release)
    refresh(args)  # IAM must work before the controller restarts and picks it up.
    if not rwx:
        configure_storage(hostname)
    install_timer(args)
    helm_upgrade(args.release, args.chart, args.release, args.values)
    # rollout statusは、pull secretが無いまま進捗期限(既定10分)を過ぎたDeploymentを
    # secretを作った後でも即座に失敗扱いにする（途中で失敗→再実行したときに起きる）。
    # Availableになるまでを待つ。
    run([*common.KUBE[:-1], "--request-timeout=660s", "-n", NS, "wait",
         "--for=condition=Available", "deployment/" + CONTROLLER, "--timeout=600s"])
    print("Generation controller Ready. Retry Gemini interview in the browser.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    environment, defaults = management.environment_defaults()
    parser.add_argument("stage", choices=["deploy", "refresh"])
    management.add_environment_option(parser, environment)
    parser.add_argument("--project", default=defaults.get("GCP_PROJECT", ""))
    parser.add_argument("--image-root", default=defaults.get("IMAGE_ROOT", ""))
    parser.add_argument("--tag", default="latest")
    # 以前のタイマー（token-file 方式）が付けていた引数。配備でタイマーを書き直すまで受け付けて無視する。
    parser.add_argument("--vertex-service-account", default="", help=argparse.SUPPRESS)
    parser.add_argument("--gemini-api-backend", default="vertex", help=argparse.SUPPRESS)
    parser.add_argument("--vertex-location", default="global")
    parser.add_argument("--gemini-model", default="gemini-3.8-flash")
    parser.add_argument("--server-node", default=defaults.get("SERVER_NODE", ""))
    parser.add_argument("--agent-node", default=defaults.get("AGENT_NODE", ""))
    parser.add_argument("--storage-verified", action="store_true")
    # 既定のnode_image(Rocky Linux)にはAppArmorが無いため空。Ubuntu系ノードを
    # 使う構成に変えたときだけ "koyorina-codex-bwrap" 等を明示で渡す。
    # 逆に、AppArmorの無いノードへ指定するとプロファイルが見つからずPodが
    # 起動しなくなる。
    parser.add_argument("--apparmor-profile", default="")
    parser.add_argument("--npm-registry", default=defaults.get("NPM_REGISTRY", ""))
    parser.add_argument("--pypi-index", default=defaults.get("PYPI_INDEX", ""))
    # Filestore等RWXクラスへ切り替えたときだけ変える。既定は今までどおり
    # local-path(RWO)で、agent-nodeへホスト名固定する。
    parser.add_argument("--storage-class", default=defaults.get("STORAGE_CLASS", "local-path"))
    parser.add_argument("--storage-access-mode", choices=("ReadWriteOnce", "ReadWriteMany"),
                        default=defaults.get("STORAGE_ACCESS_MODE", "ReadWriteOnce"))
    parser.add_argument("--node-selector", default=defaults.get("NODE_SELECTOR", ""),
                        help='例: {"workload": "koyorina-agent"}。指定するとagent-nodeより優先される。')
    parser.add_argument("--chart", default="/opt/koyorina-setup/helm/koyorina")
    parser.add_argument("--values", default="/opt/koyorina-setup/helm/koyorina-values.gce.yaml")
    parser.add_argument("--release", default=defaults.get("APP_NAME", "koyorina"))
    args = parser.parse_args()
    configure_names(args.release)
    needed = {"deploy": ("project", "image_root", "server_node", "chart", "values"),
              "refresh": ("project", "image_root")}[args.stage]
    # agent-nodeはRWO(local-path)のときだけ要る。RWXではノード固定しない。
    if args.stage == "deploy" and args.storage_access_mode != "ReadWriteMany":
        needed += ("agent_node",)
    management.require(parser, args, *needed)
    if not re.fullmatch(r"[a-z][a-z0-9-]{4,62}", args.project):
        parser.error("Invalid project")
    if not re.fullmatch(r"[a-z0-9-]+-docker\.pkg\.dev/" + re.escape(args.project) + r"/[a-z0-9._-]+", args.image_root):
        parser.error("Invalid image root")
    if args.stage == "deploy" and not Path(args.values).is_file():
        parser.error(f"values file not found: {args.values}")
    args.registry_host = args.image_root.split("/")[0]
    try:
        (deploy if args.stage == "deploy" else refresh)(args)
    except SetupError as exc:
        raise SystemExit(str(exc)) from None
    except Exception:
        raise SystemExit("Generation deployment failed; credential-bearing details withheld") from None


if __name__ == "__main__":
    main()
