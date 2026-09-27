"""GCE preview deployment. Run on the K3s server after generation deployment.

プレビューcontrollerのワークロード本体はsetup/helm/koyorinaのHelm Chartが持つ
（deploy-app-gce.pyが先に helm upgrade --install 済みという前提）。
ここに残るのは、Chartが作らないpull tokenの定期更新とストレージの前提確認だけ。
"""
import argparse
import base64
import importlib.util
import json
from pathlib import Path
import re
import shutil

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("generation", ROOT / "deploy-generation-gce.py")
generation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(generation)
management = generation.management
common = generation.common
get, kube, run, SetupError = common.get, common.kube, common.run, common.SetupError
helm_upgrade = common.helm_upgrade
NS = "koyorina-preview"
NAME = "koyorina-preview-controller"
CLIENT = "koyorina-preview-client"
PULL_SECRET = "koyorina-preview-pull"
MARKER = "# Managed by Koyorina preview deployment"
FILES = ("deploy-preview-gce.py", "deploy-generation-gce.py", "deploy-app-gce.py", "bootstrap-gce.py")


def configure_names(release):
    global NS, NAME, CLIENT, PULL_SECRET
    NS = f"{release}-preview"
    NAME = f"{release}-preview-controller"
    CLIENT = f"{release}-preview-client"
    PULL_SECRET = f"{release}-preview-pull"


def refresh(args):
    token = run(["gcloud", "auth", "print-access-token", "--project", args.project]).strip()
    if not token:
        raise SetupError("VM access token not available")
    auth = base64.b64encode(("oauth2accesstoken:" + token).encode()).decode()
    config = json.dumps({"auths": {args.image_root.split("/")[0]: {"auth": auth}}})
    management.upsert_secret(PULL_SECRET, NS,
        {".dockerconfigjson": config}, "kubernetes.io/dockerconfigjson", preserve=False)
    print("Preview pull credential refreshed.")


def install_timer(args):
    target = Path("/opt/koyorina-gce-preview")
    target.mkdir(mode=0o755, parents=True, exist_ok=True)
    for name in FILES:
        destination = target / name
        if (ROOT / name).resolve() != destination.resolve():
            shutil.copyfile(ROOT / name, destination)
        destination.chmod(0o644)
    service = MARKER + "\n" + """[Unit]
Description=Refresh Koyorina preview image credentials
After=network-online.target k3s.service
Wants=network-online.target
[Service]
Type=oneshot
User=root
Environment=PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/snap/bin
""" + (f"ExecStart=/usr/bin/python3 {target}/deploy-preview-gce.py refresh "
         f"--project {args.project} --image-root {args.image_root} "
         f"--release {args.release}\n")
    timer = MARKER + "\n" + f"""[Unit]
Description=Refresh preview image credentials every 20 minutes
[Timer]
OnBootSec=30s
OnUnitActiveSec=20min
Unit={args.release}-preview-refresh.service
[Install]
WantedBy=timers.target
"""
    for name, content in ((f"{args.release}-preview-refresh.service", service),
                          (f"{args.release}-preview-refresh.timer", timer)):
        path = Path("/etc/systemd/system") / name
        if path.exists() and not path.read_text().startswith(MARKER):
            raise SetupError("Unmanaged preview refresh unit exists")
        path.write_text(content)
    run(["systemctl", "daemon-reload"])
    run(["systemctl", "enable", "--now", f"{args.release}-preview-refresh.timer"])


def deploy(args):
    # RWX(Filestore/NFS)ではノード固定の前提そのものが無い。local-path固有の
    # 検査（workspacesラベル・nodePathMap・PVのnodeAffinity）は丸ごと飛ばす。
    rwx = getattr(args, "storage_access_mode", "ReadWriteOnce") == "ReadWriteMany"
    if not rwx and not args.storage_verified:
        raise SetupError("Verify /srv/forge mount on the agent, then pass --storage-verified")
    # RWXでは特定のagentへ固定しない（ansible.env.j2もAGENT_NODEを空にする）ので、
    # agent固有の確認はRWOのときだけ行う。
    if not rwx:
        node = get("node", args.agent_node)
        if not node or not any(c["type"] == "Ready" and c["status"] == "True"
                               for c in node.get("status", {}).get("conditions", [])):
            raise SetupError("Agent is not Ready")
        labels = node["metadata"].get("labels", {})
        hostname = labels["kubernetes.io/hostname"]
        if labels.get("storage") != "workspaces":
            raise SetupError("Agent does not have the workspace disk label")
        config = get("configmap", "local-path-config", "kube-system")
        paths = json.loads(config["data"]["config.json"]).get("nodePathMap", []) if config else []
        if not any(item["node"] == hostname and item["paths"] == ["/srv/forge/generation"] for item in paths):
            raise SetupError("Complete generation storage setup first; existing storage was not changed")
    if not get("secret", NAME, NS) or not get("secret", CLIENT, args.release):
        raise SetupError("Deploy the management application first")
    claim = get("pvc", f"{args.release}-previews", NS)
    if not rwx and claim and claim.get("spec", {}).get("volumeName"):
        pv = get("pv", claim["spec"]["volumeName"])
        terms = pv.get("spec", {}).get("nodeAffinity", {}).get("required", {}).get("nodeSelectorTerms", []) if pv else []
        matches = any(e.get("key") == "kubernetes.io/hostname" and e.get("operator") == "In"
                      and e.get("values") == [hostname] for term in terms for e in term.get("matchExpressions", []))
        if not matches:
            raise SetupError("Existing preview volume is not pinned to the selected agent")
    management.ensure_pair(NS, NAME, CLIENT, "PREVIEW_CONTROLLER_TOKEN", args.release)
    refresh(args)
    install_timer(args)
    helm_upgrade(args.release, args.chart, args.release, args.values)
    # rollout statusは、pull secretが無いまま進捗期限(既定10分)を過ぎたDeploymentを
    # secretを作った後でも即座に失敗扱いにする（途中で失敗→再実行したときに起きる）。
    # Availableになるまでを待つ。
    run([*common.KUBE[:-1], "--request-timeout=660s", "-n", NS, "wait",
         "--for=condition=Available", "deployment/" + NAME, "--timeout=600s"])
    print("Preview controller Ready. Start a preview from the browser.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    environment, defaults = management.environment_defaults()
    parser.add_argument("stage", choices=["deploy", "refresh"])
    management.add_environment_option(parser, environment)
    parser.add_argument("--project", default=defaults.get("GCP_PROJECT", ""))
    parser.add_argument("--image-root", default=defaults.get("IMAGE_ROOT", ""))
    parser.add_argument("--tag", default="latest")
    parser.add_argument("--agent-node", default=defaults.get("AGENT_NODE", ""))
    parser.add_argument("--storage-verified", action="store_true")
    # Filestore等RWXクラスへ切り替えたときだけ変える。既定は今までどおり
    # local-path(RWO)で、controller自身もagent-nodeへホスト名固定する。
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
    # 段階ごとに要るものだけを求める。refresh は認証を入れ替えるだけで、
    # ノード名もタグも使わない。全部必須にすると、定期実行が毎回失敗する
    # （そして失敗に気づけるのは、イメージを取り直す必要が出た時だけ）。
    needed = {"deploy": ("project", "image_root", "chart", "values"),
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
    try:
        (deploy if args.stage == "deploy" else refresh)(args)
    except SetupError as exc:
        raise SystemExit(str(exc)) from None
    except Exception:
        raise SystemExit("Preview deployment failed; credential-bearing details withheld") from None


if __name__ == "__main__":
    main()
