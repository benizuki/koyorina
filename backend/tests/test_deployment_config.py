import importlib.util
from pathlib import Path
import re
import subprocess
import yaml

ROOT = Path(__file__).resolve().parents[2]

_spec = importlib.util.spec_from_file_location(
    "forge_environments", ROOT / "setup/environments/load.py")
environments = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(environments)
VALUES = environments.values()


def manifest(name):
    """マニフェストはテンプレート。埋めた形で確かめる。"""
    text = environments.render((ROOT / "setup/manifest" / name).read_text(), environments.DEFAULT)
    return [document for document in yaml.safe_load_all(text) if document]


def helm_template(name):
    """setup/helm/koyorinaのテンプレートはGo templateで、{{ }}が有効なYAMLを
    壊す（例: `name: {{ .Release.Name }}-codex` はYAMLのflow mappingと解釈
    されて構文エラーになる）。`helm template`に依存せず、このリポジトリの
    テンプレートが使っている範囲（`{{ ... }}`によるインライン置換のみで、
    `{{- if/end }}`のような行レベルの制御構造は使っていない）を前提に、
    置換だけ済ませてから読む。制御構造を足したらこの前提が崩れるので、
    そのときはこの関数ごと見直す。"""
    text = (ROOT / "setup/helm/koyorina/templates" / name).read_text()
    text = re.sub(r"\{\{[^{}]*\}\}", "PLACEHOLDER", text)
    return [document for document in yaml.safe_load_all(text) if document]







def test_agent_settings_live_in_the_manifest_not_only_in_the_cluster():
    """設定はマニフェストが持つ。適用のたびにVertexの設定が消えると、Gemini経路が落ちる。"""
    documents = manifest("codex-controller.yaml")
    configmap = next(d for d in documents if d["kind"] == "ConfigMap")
    assert configmap["data"]["CONTROLLER_VERTEX_PROJECT"] == VALUES["GCP_PROJECT"]
    assert configmap["data"]["CONTROLLER_VERTEX_LOCATION"] == "global"
    assert configmap["data"]["CONTROLLER_GEMINI_API_BACKEND"] == VALUES["GEMINI_API_BACKEND"]
    # 差し込むのはイメージだけ。ConfigMapを組み立て直すと、また同じ事故になる。
    script = (ROOT / "setup/manifest/configure-codex.py").read_text()
    assert '"data": {"CONTROLLER_AGENT_IMAGE"' not in script
    # A deployment host may contain setup/ only. backend is needed solely when
    # reconciling workers that actually exist, and its absence must not stop apply.
    guard = 'if items and not backend_controller.is_file():'
    assert guard in script
    assert script.index(guard) < script.index("from backend.worker.controller import")
    assert 'items = []' in script[script.index(guard):script.index("from backend.worker.controller import")]
    assert "Podは削除しません" in script


def test_gemini_api_key_helper_never_uses_apply_or_prints_the_key():
    """Secret値をlast-applied annotationや標準出力へ残さない。"""
    script = (ROOT / "setup/manifest/configure-gemini-api-key.py").read_text()
    assert '"replace", "-f", "-"' in script and '"create", "-f", "-"' in script
    assert '"apply", "-f", "-"' not in script
    assert "print(api_key)" not in script


def test_the_agent_image_is_pinned_in_the_manifest_too():
    """agentはマニフェストに書いてある値で当てる。

    以前はクラスタにいま入っているものを引き継いでいたため、引数を書かない限り
    agentだけ何度applyしても更新されなかった。エラーは出ず、古い版が動き続ける。
    """
    configmap = next(d for d in manifest("codex-controller.yaml") if d["kind"] == "ConfigMap")
    assert re.fullmatch(r"[^\s]+/koyorina-agent@sha256:[0-9a-f]{64}",
                        configmap["data"]["CONTROLLER_AGENT_IMAGE"])
    script = (ROOT / "setup/manifest/apply.sh").read_text()
    # アプリ名は${APP_NAME}変数なので、直書きの"koyorina-agent"ではなく、
    # そのagent版のdigestを拾う仕組みがあることを見る。
    assert "-agent@sha256" in script and "APP_NAME" in script
    assert "jsonpath='{.data.CONTROLLER_AGENT_IMAGE}'" not in script
    # 差し替えは本体と同じ道具で行う。
    assert "$(APP_NAME)-agent" in (ROOT / "Makefile").read_text()


def test_the_migration_job_is_documented_as_created_not_applied():
    """generateName のJobは apply できない。テンプレートなので埋めてからでないと当たらない。

    手順に素の kubectl apply が書いてあると、その場で止まる。実際に一度止まった。
    今は手順書に手作業を書かず、apply.sh が埋めたものを create する。
    """
    migrate = (ROOT / "setup/manifest/migrate.yaml").read_text()
    assert "generateName:" in migrate and "${REGISTRY}" in migrate
    script = (ROOT / "setup/manifest/apply.sh").read_text()
    assert 'create -f "$RENDERED/migrate.yaml"' in script
    for doc in (ROOT / "setup/README.md", ROOT / "README.md", *(ROOT / "docs").rglob("*.md")):
        assert "apply -f setup/manifest/migrate.yaml" not in doc.read_text(), doc


def test_configure_codex_does_not_name_an_obsolete_migration():
    """現在のheadは進み続けるため、固定番号0002を配備のたびに案内しない。"""
    script = (ROOT / "setup/manifest/configure-codex.py").read_text()
    assert "Run migration 0002" not in script


def test_every_manifest_pins_the_same_application_image():
    """版がばらけると、どれがいま動いているのか誰にも分からなくなる。"""
    pinned = set(re.findall(r"\$\{APP_NAME\}@(sha256:[0-9a-f]{64})",
                            "\n".join(path.read_text() for path in
                                      sorted((ROOT / "setup/manifest").glob("*.yaml")))))
    assert len(pinned) == 1, pinned


def test_pushing_without_repinning_is_called_out():
    """digestを差し替えないまま当てると、成功したように見えて何も変わらない。

    黙って通ると原因に辿り着けないので、道具と注意書きの両方を用意しておく。
    """
    assert (ROOT / "setup/manifest/pin.py").is_file()
    makefile = (ROOT / "Makefile").read_text()
    assert "\npin:" in makefile and "setup/manifest/pin.py" in makefile
    # 注意書きは make help（配備の流れの説明）と apply.sh（当てる直前）に置く。
    assert "make build-push && make pin && setup/manifest/apply.sh" in makefile
    assert "make pin" in (ROOT / "setup/manifest/apply.sh").read_text()


def test_apply_script_covers_every_place_the_image_is_used():
    """1か所ずつ当てると、必ずどこかが古いまま残る。まとめて当てる経路を用意する。"""
    script = (ROOT / "setup/manifest/apply.sh").read_text()
    subprocess.run(["sh", "-n", str(ROOT / "setup/manifest/apply.sh")], check=True)
    # テンプレートを埋めたものを当てる。素のファイルを直接 kubectl へ渡さない。
    for target in ("render.py", "app.yaml", "preview.yaml", "configure-codex.py"):
        assert target in script
    assert "kubectl apply -f setup/manifest/" not in script
    # プレビューのコントローラも管理アプリと同じイメージで動く。置き去りにしない。
    preview = manifest("preview.yaml")
    images = [container["image"] for document in preview if document["kind"] == "Deployment"
              for container in document["spec"]["template"]["spec"]["containers"]]
    assert images and all("@sha256:" in image for image in images)
    # envFromのConfigMapだけが変わった場合も、常駐Podへ新しい値を取り込む。
    for deployment in ("deployment/$APP_NAME", "deployment/$APP_NAME-preview-controller",
                       "deployment/$APP_NAME-codex-controller"):
        assert f'rollout restart "{deployment}"' in script
    # DBを先に更新し、成功した場合だけ新しいアプリへ切り替える。
    migration = script.index('create -f "$RENDERED/migrate.yaml"')
    application = script.index('apply -f "$RENDERED/app.yaml"')
    assert migration < application
    assert 'wait --for=condition=complete "$MIGRATION_JOB"' in script


def test_configure_codex_rejects_a_missing_audit_ca_and_replaces_pending_workers():
    script = (ROOT / "setup/manifest/configure-codex.py").read_text()
    assert "CONTROLLER_AUDIT_CA_CONFIGMAP" in script
    assert "設定された監査CA ConfigMap" in script
    assert 'item.get("status", {}).get("phase") != "Running"' in script
    assert "replaced non-running agent pod" in script
    assert "NETWORK_POLICY_MODE=cilium ですが" in script
    assert "generation-agent-audited-egress" in script
    assert "codex-auth-audited-egress" in script


def test_preview_runtime_installs_google_requests_transport_for_every_app():
    """生成物の依存一覧に漏れがあっても、標準のGoogle認証は起動できる。"""
    path = ROOT / "setup/preview/entrypoint.sh"
    script = path.read_text()
    subprocess.run(["sh", "-n", str(path)], check=True)
    standard = script.index('"google-auth[requests]"')
    # 生成アプリはuvプロジェクト。依存は pyproject.toml から入れる。
    generated = script.index("uv pip install --quiet -r pyproject.toml")
    assert standard < generated
    # 以前の規約で作られたアプリ（pyproject.toml が無い）も起動できる。
    assert script.index("uv pip install --quiet -r backend/requirements.txt") > generated
    # 既存PVCの古いvenvも一度だけ作り直し、修正済み依存を確実に反映する。
    assert "preview-python-v3" in script


def test_preview_health_check_does_not_create_a_false_root_404_log():
    """画面の入口と生存確認をAPI専用バックエンドへ投げず、偽の404を残さない。"""
    source = (ROOT / "setup/preview/front.py").read_text()
    assert "asyncio.open_connection" in source
    assert 'UPSTREAM + "/"' not in source
    assert 'index.is_file() and frontend_path' in source
    assert 'not (DIST / "index.html").is_file()' in source


def test_preview_finishes_the_first_frontend_build_before_starting_servers():
    script = (ROOT / "setup/preview/entrypoint.sh").read_text()
    initial = script.index('node_modules/.bin/vite build --base')
    backend = script.index('uvicorn "$APP"')
    frontend = script.index('uvicorn front:app')
    assert initial < backend < frontend


def test_setup_folder_holds_every_manifest_we_apply():
    """配備に要るものはsetup配下にまとめる。散らばると、当て忘れが起きる。

    k3sの導入(旧 setup/gcp/terraform/startup/*.sh)は setup/ansible へ移した。
    """
    for path in ("setup/README.md", "setup/manifest/apply.sh", "setup/manifest/app.yaml",
                 "setup/manifest/registry.yaml", "setup/gcp/terraform/main.tf",
                 "setup/gcp/terraform/cluster.tf", "setup/ansible/site.yml",
                 "setup/ansible/roles/node-profiles/tasks/main.yml",
                 "setup/gcp/k8s/app-config.yaml"):
        assert (ROOT / path).is_file(), path
    assert not (ROOT / "deploy").exists()  # 旧い置き場は残さない
    assert not (ROOT / "setup/gcp/terraform/startup").exists()


def test_the_app_reads_the_cluster_but_cannot_change_it():
    """状態表示のために与えるのは読み取りだけ。作成・削除を与えない。"""
    documents = manifest("app.yaml")
    roles = [d for d in documents if d["kind"] == "Role"]
    assert roles, "読み取り用のRoleが無い"
    assert all(any("pods/log" in rule["resources"] for rule in role["rules"]) for role in roles)
    for role in roles:
        for rule in role["rules"]:
            if rule["resources"] == ["serviceaccounts/token"]:
                # 例外は1つだけ：WIFのために、自分自身のトークンを audience 付きで発行する。
                # 相手は名前で自分に限る。ほかのServiceAccountのトークンは作れない。
                assert rule["verbs"] == ["create"] and rule["resourceNames"] == ["koyorina-viewer"], rule
                continue
            assert set(rule["verbs"]) <= {"get", "list"}, rule
    deployment = next(d for d in documents if d["kind"] == "Deployment")
    spec = deployment["spec"]["template"]["spec"]
    assert spec["serviceAccountName"] == "koyorina-viewer"


def test_generated_application_images_stay_inside_the_cluster():
    """開発クラスタのレジストリは外へ出さない。

    本番はArtifact Registry（同じGCPプロジェクト内・脆弱性検査つき）を使うので、
    クラスタ内レジストリは開発の private 経路だけの話になる。
    """
    documents = manifest("registry.yaml")
    service = next(d for d in documents if d["kind"] == "Service")
    assert service["spec"].get("type", "ClusterIP") == "ClusterIP"
    policy = next(d for d in documents if d["kind"] == "NetworkPolicy")
    assert policy["spec"]["egress"] == []  # 外へは出さない


def test_production_uses_artifact_registry_not_an_in_cluster_one():
    """本番の置き場はArtifact Registry。クラスタ内レジストリの定義を持たない。"""
    config = next(d for d in yaml.safe_load_all((ROOT / "setup/gcp/k8s/app-config.yaml").read_text()) if d)
    assert config["data"]["APP_REGISTRY_KIND"] == "artifact"
    assert not (ROOT / "setup/gcp/k8s/registry.yaml").exists()


def test_application_image_registry_is_configurable():
    """イメージの置き場は環境変数で選ぶ。本番はArtifact Registryで脆弱性検査を付ける。"""
    from backend.domain import app_images
    development = yaml.safe_load((ROOT / "setup/gcp/k8s/app-config.yaml").read_text())
    production_host = development["data"]["APP_REGISTRY_HOST"]
    assert development["data"]["APP_REGISTRY_KIND"] == "artifact"
    app_images.validate("artifact", production_host)
    assert app_images.describe("artifact", production_host, scanning_enabled=True)["vulnerability_scanning"] is True

    k3s = next(d for d in manifest("app.yaml") if d["kind"] == "ConfigMap")
    assert k3s["data"]["APP_REGISTRY_KIND"] == "private"
    app_images.validate("private", k3s["data"]["APP_REGISTRY_HOST"])

    # 生成アプリのイメージ置き場と、本体の置き場を取り違えない。
    terraform = (ROOT / "setup/gcp/terraform/registry.tf").read_text()
    assert "containerscanning" in (ROOT / "setup/gcp/terraform/main.tf").read_text()
    assert "koyorina-builder" not in terraform  # 名前は var.name から組み立てる
    assert 'role       = "roles/artifactregistry.writer"' in terraform


def test_production_database_is_managed_and_not_reachable_from_outside():
    """本番のDBはCloud SQL。公開IPを持たせず、バックアップと復旧を効かせる。"""
    database = (ROOT / "setup/gcp/terraform/database.tf").read_text()
    assert "ipv4_enabled                                  = false" in database
    assert "deletion_protection = true" in database
    assert "point_in_time_recovery_enabled = true" in database
    assert 'ssl_mode                                      = "ENCRYPTED_ONLY"' in database
    # 接続URLはSecret Managerへ入れる。マニフェストにも出力にも平文を置かない。
    assert "google_secret_manager_secret" in database
    outputs = (ROOT / "setup/gcp/terraform/outputs.tf").read_text()
    assert "random_password" not in outputs and "secret_data" not in outputs




def test_generation_nodes_get_the_reviewed_seccomp_profile():
    """生成ノードには、手元で検証したものと同じプロファイルが入る。

    kubeletはノードのファイルしか読まない。k3sの導入・ノードへの配置は
    setup/ansible が担う（Terraformはインスタンスを作るだけで、
    startup-scriptによる配置は廃止した）。埋め込む中身がk3sで検証したものと
    同じであることを、ここで縛る。
    """
    cluster = (ROOT / "setup/gcp/terraform/cluster.tf").read_text()
    assert "startup-script" not in cluster
    role = (ROOT / "setup/ansible/roles/node-profiles/tasks/main.yml").read_text()
    assert "/var/lib/kubelet/seccomp/koyorina/codex-bwrap-amd64-v1.json" in role
    assert "apparmor" in role.lower()
    # roleが実際に指す先が実在することまで確かめる（綴りだけを縛らない）。
    referenced = re.findall(r'src:\s*"\{\{ playbook_dir \}\}/\.\./([^"\s]+)"', role)
    assert len(referenced) == 2, referenced
    for relative in referenced:
        assert (ROOT / "setup" / relative).resolve().is_file(), relative


def test_agent_uses_codex_no_proc_fallback_for_restricted_pods():
    """restricted Podではprocだけ省略し、他のbwrap隔離は維持する。"""
    wrapper = (ROOT / "setup/manifest/codex-bwrap-no-proc").read_text()
    dockerfile = (ROOT / "setup/manifest/Dockerfile.agent").read_text()
    assert 'os.execv("/usr/bin/bwrap"' in wrapper
    assert 'args[index] == "--proc"' in wrapper
    # python:*-slim のPythonは /usr/local/bin にだけある。/usr/bin/python3 を指すと
    # 入口そのものが起動できず（no such file or directory）、Codexの閉じ込めが全滅する。
    assert "FROM python:" in dockerfile and wrapper.startswith("#!/usr/local/bin/python3\n")
    assert "COPY setup/manifest/codex-bwrap-no-proc /usr/local/bin/bwrap" in dockerfile
    assert "chmod 0555 /usr/local/bin/bwrap" in dockerfile


def test_no_virtual_machine_is_reachable_from_the_internet():
    """入口はロードバランサだけ。VMに外部IPを付けず、直接の口も開けない。"""
    terraform = (ROOT / "setup/gcp/terraform/cluster.tf").read_text()
    assert "access_config" not in terraform
    # LBの通り道とヘルスチェック、保守用のIAPだけを通す。
    assert 'source_ranges = [var.proxy_subnet_cidr, "35.191.0.0/16", "130.211.0.0/22"]' in terraform
    assert 'source_ranges = ["35.235.240.0/20"]' in terraform


def test_the_load_balancer_is_protected_and_terminates_tls():
    """守りはLBの手前で効かせる。平文(80番)は受けない(リダイレクトも持たない。管理を
    減らすため、意図的にport 80のリスナー自体を持たない設計にした)。"""
    balancer = (ROOT / "setup/gcp/terraform/loadbalancer.tf").read_text()
    assert "google_compute_region_target_https_proxy" in balancer
    assert "google_compute_forwarding_rule" in balancer
    assert '"80"' not in balancer
    assert "security_policy = var.cloud_armor_enabled" in balancer
    # 証明書は取得も更新も任せる。手で入れ替える運用にしない。
    assert "google_certificate_manager_certificate" in balancer

    armor = (ROOT / "setup/gcp/terraform/cloud-armor.tf").read_text()
    # ヘルスチェックは国の制限より先に通す。順番を崩すとLBが不健全と判断する。
    assert armor.index("35.191.0.0/16") < armor.index("origin.region_code != 'JP'")
    for rule in ("sqli-v422-stable", "xss-v422-stable", "rce-v422-stable",
                 "protocolattack-v422-stable", "sessionfixation-v422-stable"):
        assert rule in armor
    assert "rate_based_ban" in armor and 'exceed_action    = "deny(429)"' in armor
    # 誤検知で使えなくなる方が困る。まず記録から始める。
    variables = (ROOT / "setup/gcp/terraform/variables.tf").read_text()
    assert 'variable "cloud_armor_waf_preview"' in variables


def test_every_setting_has_a_place_in_the_example_file():
    """設定は1か所にまとめる。変数を足して書き忘れると、既定のまま適用される。"""
    import re
    terraform = ROOT / "setup/gcp/terraform"
    declared = set(re.findall(r'variable "([a-z_]+)"', (terraform / "variables.tf").read_text()))
    example = (terraform / "terraform.tfvars.example").read_text()
    written = set(re.findall(r"^([a-z_]+)\s*=", example, re.M))
    assert declared == written, declared ^ written
    # 実物は履歴に入れない。実環境のIP範囲を書くため。
    assert "**/terraform.tfvars" in (ROOT / ".gitignore").read_text()


def test_agents_can_be_scaled_and_the_workspace_disk_has_one_home():
    """台数は0からnまで。作業領域のディスクは1台にしか付かない（RWO、Filestore無効時）。"""
    variables = (ROOT / "setup/gcp/terraform/variables.tf").read_text()
    assert "var.agent_count >= 0 && var.agent_count <= 8" in variables
    cluster = (ROOT / "setup/gcp/terraform/cluster.tf").read_text()
    assert "count        = var.agent_count" in cluster
    # 1台目にだけ付ける。k3sの導入・ノードラベルの付与は setup/ansible が担う
    # （Terraformはenable_filestoreでディスクの有無を切り替えるだけ）。
    assert 'count = var.enable_filestore ? 0 : 1' in cluster
    assert "google_compute_disk.workspaces[0].id" in cluster


def test_filestore_is_a_single_instance_and_off_by_default():
    """FilestoreはPVCやテナントの数に関わらず常に1つだけ。既定は無効。"""
    variables = (ROOT / "setup/gcp/terraform/variables.tf").read_text()
    assert 'variable "enable_filestore"' in variables
    assert 'default     = false' in variables.split('variable "enable_filestore"')[1].split("\n\n")[0]
    filestore = (ROOT / "setup/gcp/terraform/filestore.tf").read_text()
    # インスタンス定義は1つだけ（テナント数などに連動するfor_each/count>1が無い）。
    assert filestore.count('resource "google_filestore_instance"') == 1
    assert "count    = var.enable_filestore ? 1 : 0" in filestore
    assert "for_each" not in filestore


def test_manifests_stay_at_the_minimum_we_apply():
    """当てるものだけを置く。一度きりのJobや当て済みのパッチを残さない。"""
    manifests = sorted(p.name for p in (ROOT / "setup/manifest").glob("*.yaml"))
    assert manifests == ["app.yaml", "codex-controller.yaml", "gateway.yaml",
                         "migrate.yaml", "namespace.yaml", "postgres.yaml",
                         "preview.yaml", "registry.yaml"], manifests
    # 移行は版ごとにファイルを作らない。どこまで当たったかはDBが持つ。
    migrate = (ROOT / "setup/manifest/migrate.yaml").read_text()
    assert 'generateName: "${APP_NAME}-migrate-"' in migrate
    assert "'upgrade', 'head'" in migrate


def test_no_terraform_state_is_kept_in_the_repository():
    """stateは置かない。手元にしか無い状態を作ると、他の人が触れなくなる。"""
    assert not list(ROOT.rglob("terraform.tfstate*"))
    assert not list(ROOT.rglob("*.tfplan"))
    # 新しいスタックはGCSに置く。
    assert 'backend "gcs"' in (ROOT / "setup/gcp/terraform/backend.tf").read_text()


def test_generated_applications_cannot_reach_the_metadata_server():
    """メタデータサーバに触れると、生成アプリがVMの身元でトークンを取れてしまう。

    オンプレでは当たらないが、GCEでは当たる。塞いだまま動くことを、ここで縛る。
    """
    for name in ("preview.yaml", "codex-controller.yaml"):
        policies = [d for d in yaml.safe_load_all((ROOT / "setup/manifest" / name).read_text())
                    if d and d["kind"] == "NetworkPolicy"]
        wide = [rule for policy in policies for entry in policy["spec"].get("egress", [])
                for destination in entry.get("to", [])
                for rule in [destination.get("ipBlock")] if rule and rule["cidr"] == "0.0.0.0/0"]
        assert wide, name
        for rule in wide:
            assert "169.254.0.0/16" in rule["except"], (name, rule)


def test_the_storage_budget_fits_the_shared_workspace_everywhere():
    """枠が共有作業場所より小さいと、生成Podが一つも作れない。

    prepare() はPVCを先に作る。ここで拒まれるとServiceもPodも作られず、
    「実行環境へ届かない」としか出ない。本番だけ上限を上げ忘れており、
    実際にPodが復帰しなくなった。環境ごとに手で揃える形にしない。
    """
    from backend.worker.controller import ControllerSettings

    def gigabytes(value):
        assert value.endswith("Gi"), f"Gi 以外の単位は想定していない: {value}"
        return int(value[:-2])

    wanted = gigabytes(ControllerSettings.model_fields["workspaces_size"].default)
    quotas = {}

    codex = yaml.safe_load_all((ROOT / "setup/manifest/codex-controller.yaml").read_text())
    for document in codex:
        if document and document.get("kind") == "ResourceQuota":
            quotas["開発"] = document["spec"]["hard"]

    for document in helm_template("codex-controller-namespace.yaml"):
        if document.get("kind") == "ResourceQuota":
            quotas["本番"] = document["spec"]["hard"]

    assert set(quotas) == {"開発", "本番"}, "どちらかの ResourceQuota を読めていない"
    for name, hard in quotas.items():
        # 共有ぶんに加えて、利用者ごとのPVC（2Gi）が並ぶ。ぴったりでは足りない。
        assert gigabytes(hard["requests.storage"]) >= wanted + 4, (
            f"{name}の requests.storage が共有作業場所（{wanted}Gi）に対して小さい")
        assert int(hard["persistentvolumeclaims"]) >= 4, f"{name}のPVC本数が少なすぎる"
        # 生成Podは依存の導入と vite build / vue-tsc をこのなかで走らせる。
        # 上限の合計がPod数×Podの上限に届いていないと、数人が同時に使った時点で
        # Podが作られなくなる（作業場所PVCで同じ形の事故が起きている）。
        limit = gigabytes(pod_memory_limit())
        assert gigabytes(hard["limits.memory"]) >= int(hard["pods"]) * limit, (
            f"{name}の limits.memory が Pod {hard['pods']}個ぶん（各{limit}Gi）に足りない")


def pod_memory_limit():
    from uuid import uuid4
    from backend.worker.controller import ControllerSettings, resources
    settings = ControllerSettings(token="x" * 40,
        agent_image="registry.example.com/koyorina-agent@sha256:" + "a" * 64)
    pod = resources(uuid4(), settings, "codex")["pods"]
    return pod["spec"]["containers"][0]["resources"]["limits"]["memory"]
