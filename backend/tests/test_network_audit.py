import importlib.util
import json
import logging
from pathlib import Path
from uuid import uuid4

import yaml

from backend.tests.test_resumption import SPEC
from backend.worker.agent import Agent, AgentSettings, GenerationInput
from backend.worker.controller import ControllerSettings, resources


ROOT = Path(__file__).resolve().parents[2]
OBS = ROOT / "contrib/observability"
spec = importlib.util.spec_from_file_location(
    "forge_environments", ROOT / "setup/environments/load.py")
environments = importlib.util.module_from_spec(spec)
spec.loader.exec_module(environments)


def rendered(name, environment="dev"):
    text = environments.render((OBS / name).read_text(), environment)
    return [item for item in yaml.safe_load_all(text) if item]


def test_generation_policies_allow_public_https_but_keep_private_networks_closed():
    generated, auth = rendered("cilium-policy.yaml")
    assert generated["kind"] == auth["kind"] == "CiliumNetworkPolicy"
    assert auth["spec"]["endpointSelector"]["matchLabels"] == {
        "app": "koyorina-codex-agent",
        "forge-route": "codex",
        "forge-purpose": "authentication",
        "forge-network-policy": "cilium",
    }
    text = (OBS / "cilium-policy.yaml").read_text()
    assert "toFQDNs" not in text and "terminatingTLS" not in text
    for policy in (generated, auth):
        public = next(rule for rule in policy["spec"]["egress"] if rule.get("toCIDRSet"))
        cidr = public["toCIDRSet"][0]
        assert cidr["cidr"] == "0.0.0.0/0"
        assert {"10.0.0.0/8", "127.0.0.0/8", "169.254.0.0/16",
                "172.16.0.0/12", "192.168.0.0/16"} <= set(cidr["except"])
        assert public["toPorts"] == [{"ports": [{"port": "443", "protocol": "TCP"}]}]


def test_hubble_redacts_secrets_and_collector_has_bounded_durable_queue():
    values = yaml.safe_load((OBS / "cilium-values.yaml").read_text())
    assert values["hubble"]["export"]["static"]["allowList"] == [
        '{"source_pod":["koyorina-codex/"]}'
    ]
    redaction = values["hubble"]["redact"]
    assert redaction["enabled"] and redaction["http"]["urlQuery"]
    assert redaction["http"]["headers"]["allow"] == []
    collector = next(item for item in rendered("collector.yaml")
                     if item["kind"] == "ConfigMap" and item["metadata"]["name"] == "audit-collector")
    config = yaml.safe_load(collector["data"]["splunk_hec.yaml"])
    # Eight receiver/exporter databases × 256MiB = a node-wide 2GiB cap.
    assert config["extensions"]["file_storage"]["max_size"] == 268_435_456
    exporters = config["exporters"]
    assert all(value["sending_queue"]["storage"] == "file_storage" for value in exporters.values())
    assert all(value["tls"]["insecure"] == "${env:SPLUNK_HEC_INSECURE:-false}"
               for value in exporters.values())
    assert "Authorization" not in collector["data"]["splunk_hec.yaml"]
    splunk_config = collector["data"]["splunk_hec.yaml"]
    assert 'id: lifecycle-only' in splunk_config
    assert '(SYN|FIN|RST)' in splunk_config
    assert 'trace_reason' in splunk_config and 'NEW' in splunk_config
    assert 'id: dns-query-only' in splunk_config
    assert 'filelog/cilium_dns' in splunk_config
    assert 'koyorina:cilium_dns' in splunk_config
    assert 'lost_events?' in splunk_config
    collector_ds = next(item for item in rendered("collector.yaml") if item["kind"] == "DaemonSet")
    init_caps = collector_ds["spec"]["template"]["spec"]["initContainers"][0]["securityContext"]["capabilities"]
    assert "FOWNER" in init_caps["add"]


def test_app_can_read_sanitized_recent_flows_without_host_file_access():
    resources = rendered("collector.yaml")
    reader = next(item for item in resources
                  if item["kind"] == "ConfigMap" and item["metadata"]["name"] == "network-audit-reader")
    script = reader["data"]["flows"]
    assert "traffic_direction" in script and "is_reply" in script
    assert '"FIN":true' in script and '"RST":true' in script
    assert '"destination_port":53' in script
    assert "tail -c 8388608" in script and "tail -n 800" in script
    role = next(item for item in resources if item["kind"] == "Role")
    assert role["rules"] == [{"apiGroups": [""], "resources": ["pods", "pods/proxy"],
                               "verbs": ["get", "list"]}]
    binding = next(item for item in resources if item["kind"] == "RoleBinding")
    assert binding["subjects"][0] == {"kind": "ServiceAccount", "name": "koyorina-viewer",
                                      "namespace": "koyorina"}
    daemonset = next(item for item in resources if item["kind"] == "DaemonSet")
    sidecar = next(item for item in daemonset["spec"]["template"]["spec"]["containers"]
                   if item["name"] == "network-audit-reader")
    assert sidecar["securityContext"]["readOnlyRootFilesystem"] is True
    assert sidecar["ports"][0]["containerPort"] == 8081
    cluster_reader = (ROOT / "backend/core/cluster.py").read_text()
    assert 'f"{pod}:8081"' in cluster_reader
    assert 'f"{pod}:audit-read"' not in cluster_reader


def test_splunk_http_requires_an_explicit_opt_in_and_supports_ip_destinations():
    script = (OBS / "configure-audit.sh").read_text()
    migration = (OBS / "migrate-dev-cilium.sh").read_text()
    local_config = (OBS / "config.local.sh.example").read_text()
    assert 'SPLUNK_HEC_ALLOW_HTTP:-false' in script
    assert "source \"$CONFIG_FILE\"" in migration
    assert 'configure-audit.sh\" --check' in migration
    assert "SPLUNK_HEC_ALLOW_HTTP=true" in local_config
    assert 'HEC_DESTINATION="toCIDR: [\\"$HEC_CIDR\\"]"' in script
    assert 'HEC_DESTINATION="toFQDNs:' in script
    assert "SPLUNK_HEC_CA_FILE=$ORIGIN_CA_BUNDLE" in script
    assert 'data["ca.crt"]' in script
    assert "rollout status" in migration and "daemonset/audit-collector" in migration
    assert "rollout restart" in script and "daemonset/audit-collector" in script


def test_audit_export_can_be_disabled_without_an_external_sink():
    script = (OBS / "configure-audit.sh").read_text()
    migration = (OBS / "migrate-dev-cilium.sh").read_text()
    assert 'EXPORTER" = disabled' in script
    assert "delete daemonset audit-collector" in script
    assert "delete ciliumnetworkpolicy audit-collector-egress" in script
    assert 'AUDIT_LOG_EXPORTER)" != disabled' in migration


def test_network_canary_satisfies_namespace_resource_quota():
    script = (OBS / "canary.sh").read_text()
    assert "requests: {cpu: 10m, memory: 32Mi}" in script
    assert "limits: {cpu: 100m, memory: 128Mi}" in script
    assert "runAsUser: 10001" in script
    assert "SSL_CERT_FILE" in script and "ssl.create_default_context" in script


def test_audit_ca_is_a_valid_signing_ca_and_hubble_filters_follow_export_shape():
    tls_setup = (OBS / "configure-tls.sh").read_text()
    assert "basicConstraints=critical,CA:TRUE,pathlen:0" in tls_setup
    assert "keyUsage=critical,keyCertSign,cRLSign" in tls_setup
    collector = (OBS / "collector.yaml").read_text()
    assert collector.count("parse_to: body") >= 2
    assert "body.flow.l7 != nil" in collector
    assert "dns-query-only" in collector and "filelog/cilium_dns" in collector
    assert "body.l7" not in collector
    splunk = OBS / "splunk/koyorina-network-audit/default"
    searches = (splunk / "savedsearches.conf").read_text()
    dashboard = (splunk / "data/ui/views/network_audit.xml").read_text()
    props = (splunk / "props.conf").read_text()
    assert "flow.l7.dns.query" in searches and "flow.IP.destination" in searches
    assert "flow.verdict" in dashboard and "flow.l7.http.code" in dashboard
    assert "flow.l7.http.url" in props and "EXTRACT-tenant_id" in props


def test_development_workers_receive_audit_ca_but_auth_workers_do_not():
    tenant = uuid4()
    settings = ControllerSettings(_env_file=None, token="x" * 40,
                                  agent_image="registry.example.com/koyorina-agent@sha256:" + "a" * 64,
                                  environment="dev", network_policy_mode="cilium",
                                  audit_ca_configmap="koyorina-audit-ca")
    worker = resources(uuid4(), settings, tenant=str(tenant))["pods"]
    assert worker["metadata"]["labels"]["forge-network-policy"] == "cilium"
    assert any(item["name"] == "prepare-audit-ca" for item in worker["spec"]["initContainers"])
    env = {item["name"]: item for item in worker["spec"]["containers"][0]["env"]}
    assert env["AGENT_TENANT_ID"]["value"] == str(tenant)
    assert env["NODE_EXTRA_CA_CERTS"]["value"].endswith("ca-bundle.crt")
    auth = resources(uuid4(), settings, auth_only=True)["pods"]
    assert "initContainers" not in auth["spec"]
    assert not any(item["name"] == "audit-ca" for item in auth["spec"]["volumes"])


def test_job_audit_event_excludes_prompt_specification_and_source(caplog, tmp_path):
    tenant, user = uuid4(), uuid4()
    agent = Agent(AgentSettings(_env_file=None, token="x" * 40, user_id=user,
                                tenant_id=tenant, root=tmp_path, pod_name="worker-1"))
    payload = GenerationInput(job_id=uuid4(), project_id=uuid4(), specification=SPEC,
                              instruction="DO_NOT_LOG_THIS", requested_by="PRIVATE_PERSON",
                              generator="gemini")
    with caplog.at_level(logging.INFO, logger="uvicorn.error"):
        agent.audit_generation("generation.job.start", payload)
        agent.audit_generation("generation.job.end", payload, outcome="generated")
    lines = [record.getMessage() for record in caplog.records
             if record.getMessage().startswith("koyorina_audit ")]
    assert len(lines) == 2
    result = json.loads(lines[-1].removeprefix("koyorina_audit "))
    assert result["tenant_id"] == str(tenant) and result["user_id"] == str(user)
    assert result["provider"] == "gemini" and result["outcome"] == "generated"
    joined = "\n".join(lines)
    assert "DO_NOT_LOG_THIS" not in joined and "PRIVATE_PERSON" not in joined
    assert SPEC.name not in joined


def test_production_cni_is_unchanged_and_legacy_policy_is_scoped():
    """既定はlegacy(Cilium未導入のCNI向け)のまま。Ansible経由の本番はgroup_vars
    （運用者ごとのlocal.ymlを含む）から値が決まるため、setup/environments/の
    prod.envは前提にしない——ここでは「明示的に上書きしない限りlegacyのまま」
    という既定値の安全側を検査する。"""
    all_defaults = (ROOT / "setup/ansible/group_vars/all/defaults.yml").read_text()
    assert "forge_network_policy_mode: legacy" in all_defaults
    manifest = environments.render((ROOT / "setup/manifest/codex-controller.yaml").read_text(), "dev")
    policies = [item for item in yaml.safe_load_all(manifest)
                if item and item.get("kind") == "NetworkPolicy"]
    public = next(item for item in policies if item["metadata"]["name"] == "codex-agent-codex-egress")
    expressions = public["spec"]["podSelector"]["matchExpressions"]
    assert {"key": "forge-network-policy", "operator": "In", "values": ["legacy"]} in expressions


def test_cilium_migration_uses_the_pinned_cli_without_external_helm():
    migration = (OBS / "migrate-dev-cilium.sh").read_text()
    tls_setup = (OBS / "configure-tls.sh").read_text()
    rollback = (OBS / "rollback-dev-cilium.sh").read_text()
    assert "command -v helm" not in migration
    assert "helm upgrade" not in migration and "helm uninstall" not in rollback
    assert 'install --version 1.20.2' in migration
    assert '--set k8sServiceHost="$K8S_API_HOST"' in migration
    assert "jsonpath={.status.addresses" in migration
    assert "ip link delete flannel.1" in migration
    assert migration.index('uncordon "$SERVER_NODE"') < migration.index(
        "rollout status deployment/cilium-operator"
    )
    assert migration.index('uncordon "${ALL_NODE_NAMES[@]}"') < migration.index(
        "rollout status deployment/hubble-relay"
    )
    assert "rollout restart daemonset/cilium" in migration
    assert "connectivity test --log-check-only-test-time" in migration
    assert "SUDO_KEEPALIVE_PID" in migration
    assert 'CONNECTIVITY_MARKER="$BACKUP/connectivity-ok"' in migration
    assert "MODE == --finish" in migration
    assert "finish_migration" in migration
    assert "移行処理が${LINENO}行目で停止" in migration
    assert "codex-agent-codex-egress --ignore-not-found" in migration
    assert "command -v openssl" in migration
    assert "sudo dnf install -y openssl" in tls_setup
    assert "apply --server-side" in tls_setup
    assert "koyorina-audit-origin-roots" in tls_setup
    assert 'uninstall --wait' in rollback
    assert "既存バックアップを保持してCilium導入から再開" in migration
