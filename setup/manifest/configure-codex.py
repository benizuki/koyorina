"""Operator-only: install code images and internal token without printing credentials."""
import argparse
import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import yaml

# ドメインやイメージの置き場は setup/environments/ が正。ここには持たせない。
_spec = importlib.util.spec_from_file_location(
    "forge_environments", Path(__file__).resolve().parents[1] / "environments" / "load.py")
environments = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(environments)


# 当てる先は KUBE_CONTEXT で選ぶ（既定は開発）。apply.sh から引き継がれる。
CONTEXT = os.environ.get("KUBE_CONTEXT", "default")


def kubectl(args, payload=None, *, optional=False):
    result = subprocess.run(["kubectl", "--context", CONTEXT, *args],
        input=json.dumps(payload) if payload is not None else None, text=True, capture_output=True)
    if result.returncode:
        if optional and "NotFound" in result.stderr:
            return None
        raise RuntimeError("Kubernetes操作に失敗しました。認証・対象リソースを確認してください。")
    return json.loads(result.stdout) if result.stdout.strip().startswith("{") else None


def apply(document):
    kubectl(["apply", "-f", "-"], document)


# probe の終了コード。待てば済むもの（WAITING）と、人が見ないと直らないものを分ける。
REASONS = {2: "生成中です", 5: "ChatGPTのログイン手続き中です",
           3: "稼働状態を返しませんでした", 4: "応答がありません"}
WAITING = (2, 5)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--app-image", required=True)
    parser.add_argument("--agent-image", required=True)
    parser.add_argument("--environment", default=os.environ.get("ENV", environments.DEFAULT),
                        choices=environments.names())
    args = parser.parse_args()
    registry = environments.required(args.environment, "REGISTRY")
    app_name = environments.required(args.environment, "APP_NAME")
    codex_ns = f"{app_name}-codex"
    for image, repository in [(args.app_image, app_name), (args.agent_image, f"{app_name}-agent")]:
        if not re.fullmatch(re.escape(registry) + "/" + repository + r"@sha256:[0-9a-f]{64}", image):
            parser.error(f"{registry} のビルド済みイメージを、sha256 digestで指定してください。")
    # マニフェストはテンプレート。埋めてから読む。埋め残しは render 側で止まる。
    documents = [document for document in yaml.safe_load_all(environments.render(
        Path(__file__).with_name("codex-controller.yaml").read_text(), args.environment)) if document]
    apply(documents[0])
    existing = kubectl(["-n", codex_ns, "get", "secret", f"{app_name}-codex-controller", "-o", "json"], optional=True)
    if existing:
        token = base64.b64decode(existing["data"]["token"]).decode()
    else:
        token = secrets.token_urlsafe(48)
        # create, not apply: no last-applied annotation containing token material.
        kubectl(["create", "-f", "-"], {"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
            "metadata": {"name": f"{app_name}-codex-controller", "namespace": codex_ns}, "stringData": {"token": token}})
    management = kubectl(["-n", app_name, "get", "secret", f"{app_name}-codex-client", "-o", "json"], optional=True)
    if management:
        if base64.b64decode(management["data"]["CODEX_CONTROLLER_TOKEN"]).decode() != token:
            raise RuntimeError("既存サービス間鍵が一致しません。上書きせず停止しました。")
    else:
        kubectl(["create", "-f", "-"], {"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
            "metadata": {"name": f"{app_name}-codex-client", "namespace": app_name},
            "stringData": {"CODEX_CONTROLLER_TOKEN": token}})
    # 設定はマニフェスト側が持ち、ここではイメージだけを差し込む。
    # ConfigMapを丸ごと置き換えると、Vertexの設定が消えてGemini経路が落ちる。
    configmap = next(d for d in documents if d["kind"] == "ConfigMap")
    configmap["data"]["CONTROLLER_AGENT_IMAGE"] = args.agent_image
    audit_ca = configmap["data"].get("CONTROLLER_AUDIT_CA_CONFIGMAP", "").strip()
    if audit_ca and not kubectl(["-n", codex_ns, "get", "configmap", audit_ca, "-o", "json"], optional=True):
        raise RuntimeError(
            f"設定された監査CA ConfigMap {codex_ns}/{audit_ca} がありません。"
            "TLS監査を使う場合は contrib/observability/configure-tls.sh を先に実行し、"
            "使わない場合は AUDIT_CA_CONFIGMAP を空にしてください。")
    policy_mode = configmap["data"].get("CONTROLLER_NETWORK_POLICY_MODE", "legacy").strip()
    if policy_mode == "cilium":
        missing = [name for name in ("generation-agent-audited-egress", "codex-auth-audited-egress")
                   if not kubectl(["-n", codex_ns, "get", "ciliumnetworkpolicy", name, "-o", "json"],
                                  optional=True)]
        if missing:
            raise RuntimeError(
                "NETWORK_POLICY_MODE=cilium ですが、生成Pod用CiliumNetworkPolicyがありません: "
                + ", ".join(missing)
                + "。監査基盤を構築していない場合は NETWORK_POLICY_MODE=legacy にしてください。")
    apply(configmap)
    # envFromで読むConfigMapは既存Podへ自動反映されない。内容のdigestをPod templateへ
    # 入れ、agent imageや生成設定が変わったときだけcontrollerを再起動する。
    controller = next(d for d in documents if d["kind"] == "Deployment"
                      and d["metadata"]["name"] == f"{app_name}-codex-controller")
    annotations = controller["spec"]["template"]["metadata"].setdefault("annotations", {})
    serialized = json.dumps(configmap["data"], sort_keys=True, separators=(",", ":")).encode()
    annotations[f"{app_name}/config-sha256"] = hashlib.sha256(serialized).hexdigest()
    # 利用者ごとのPodは、コントローラが「無ければ作る」だけで作り直さない。
    # 古い版が残ると、生成側の修正が本人の環境にいつまでも届かない。ここで片付ける。
    # 設定はPodの環境変数に焼き付く。イメージだけでなく、設定の反映も見る。
    for document in documents[1:]:
        if document["kind"] == "ConfigMap":
            continue  # イメージを差し込んだものを上で適用済み。
        if document["kind"] == "Deployment":
            document["spec"]["template"]["spec"]["containers"][0]["image"] = args.app_image
        apply(document)

    # Podの入れ替えはここまで終えてから。先に消すと、まだ古いコントローラが
    # ラベルの無いPodを作り直しうる。そのPodは下りがDNSだけになり生成できない。
    # ラベル値自体はテナントPodの命名規則(backend/domain/tenant_storage.py)に
    # 揃えて固定。namespaceだけがAPP_NAME連動。
    pods = kubectl(["-n", codex_ns, "get", "pods",
                    "-l", "app=koyorina-codex-agent", "-o", "json"]) or {}
    kubectl(["-n", codex_ns, "rollout", "status", f"deployment/{app_name}-codex-controller", "--timeout=120s"])
    # Reconcile existing workers into the shared user+tenant shape. Retain the PVCs and
    # credential Secrets; idle legacy Pods are deleted and recreated on the next login.
    items = pods.get("items", [])
    backend_controller = (Path(__file__).resolve().parents[2]
                          / "backend" / "worker" / "controller.py")
    if items and not backend_controller.is_file():
        names = ", ".join(item["metadata"]["name"] for item in items)
        print("配備ホストにbackendソースがないため、既存の生成Podの差分確認を省略します。"
              f" 対象: {names}")
        print("Podは削除しません。Controllerは次回のPod作成時に最新設定を使用します。")
        items = []
    if items:
        import sys
        from uuid import UUID
        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
        from backend.worker.controller import ControllerSettings, resources
        values = {key.removeprefix("CONTROLLER_").lower(): value for key, value in configmap["data"].items()}
        settings = ControllerSettings(**values, token=token)
    kept = []
    for item in items:
        labels = item["metadata"]["labels"]
        user = UUID(labels["forge-user"])
        route = labels["forge-route"]
        tenant = labels.get("forge-tenant")
        desired = resources(user, settings, route, tenant=tenant, auth_only=not tenant)
        wanted = desired["pods"]["spec"]
        current = item["spec"]
        if (current["containers"][0]["image"] == args.agent_image
                and current["containers"][0].get("env") == wanted["containers"][0]["env"]
                and current["containers"][0].get("volumeMounts") == wanted["containers"][0]["volumeMounts"]
                and current["securityContext"] == wanted["securityContext"]
                and item["metadata"].get("labels", {}).get("forge-route") == route):
            continue
        # Pending/FailedのPodには実行中の生成がない。execによるbusy確認もできないため、
        # 古い設定のまま残さずここで交換する。特に存在しないConfigMapのmount失敗は、
        # 待ち続けてもReadyにならない。
        ready = any(condition.get("type") == "Ready" and condition.get("status") == "True"
                    for condition in item.get("status", {}).get("conditions", []))
        if item.get("status", {}).get("phase") != "Running" or not ready:
            kubectl(["-n", codex_ns, "delete", "pod", item["metadata"]["name"],
                     "--wait=true", "--timeout=60s"])
            kubectl(["-n", codex_ns, "delete", "service", item["metadata"]["name"]], optional=True)
            print("replaced non-running agent pod:", item["metadata"]["name"])
            continue
        # A read-only local status request. Never print the bearer token or stop
        # a running generation as an incidental part of an image update.
        # 終了コードで理由を分ける。「生成中」と「確認できない」は対処が違う。
        # ひとつに潰すと、待てば済むのか、壊れているのかが操作する側に伝わらない。
        probe = """import os, sys, json, urllib.request, urllib.error
for endpoint in ('runtime', 'account'):
 try:
  req = urllib.request.Request('http://127.0.0.1:8080/' + endpoint, headers={'Authorization': 'Bearer ' + os.environ['AGENT_TOKEN']})
  with urllib.request.urlopen(req, timeout=35) as response: status = json.load(response)
 except urllib.error.HTTPError as error:
  if error.code == 404: continue
  print(endpoint, 'HTTP', error.code, file=sys.stderr); raise SystemExit(3)
 except Exception as error:
  print(endpoint, type(error).__name__, file=sys.stderr); raise SystemExit(4)
 if status.get('busy'): raise SystemExit(2)
 if status.get('login'): raise SystemExit(5)
 break
else:
 print('no status endpoint answered', file=sys.stderr); raise SystemExit(3)
"""
        check = subprocess.run(["kubectl", "--context", CONTEXT, "-n", codex_ns,
                                "exec", item["metadata"]["name"], "--", "python", "-c", probe],
                               capture_output=True, text=True)
        if check.returncode:
            # 生成中なら、待てば次回に入れ替わる。異常なら人が見ないと直らない。
            waiting = check.returncode in WAITING
            reason = REASONS.get(check.returncode, "稼働状態を確認できませんでした")
            detail = "" if waiting else (check.stderr.strip().splitlines() or [""])[-1][:200]
            kept.append((item["metadata"]["name"], reason, detail))
            continue
        kubectl(["-n", codex_ns, "delete", "pod", item["metadata"]["name"], "--wait=true", "--timeout=60s"])
        # Current controllers relay to the trusted Pod IP. Remove any per-worker Service
        # left by the former DNS routing model while replacing its Pod.
        kubectl(["-n", codex_ns, "delete", "service", item["metadata"]["name"]],
                optional=True)
        # The controller recreates missing workers on the next request.
        print("replaced idle agent pod:", item["metadata"]["name"])

    # 入れ替えられなかったPodは、前の版のまま動き続ける。コントローラは「無ければ作る」
    # だけなので、黙って進むと直したはずの不具合がその人にだけ残る。必ず名前を出す。
    for name, reason, detail in kept:
        print(f"skipped: {name} — {reason}" + (f" / {detail}" if detail else ""))
    if kept:
        print(f"上の {len(kept)} 個のPodは前の版のままです。"
              "生成が終わってから setup/manifest/apply.sh をもう一度実行してください。")
    print("生成Controllerの設定を反映しました。")
    # 生成中は待てば済むので止めない。確認できないほうは異常なので、ここで落とす。
    if any(detail for _, _, detail in kept):
        raise SystemExit("稼働状態を確認できないPodがあります。上の一覧を確認してください。"
                         "（このあとの rollout 確認は実行されていません）")


if __name__ == "__main__":
    main()
