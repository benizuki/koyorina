"""開発クラスタのマニフェスト。当てる前に、崩れていないかをここで見る。

以前は setup/ に置いていて、テストとして実行されていなかった。動かないテストは
書いていないのと同じなので、ここへ移した。移した時点で、実際の内容とずれていた
ところも直している。
"""
import importlib.util
from pathlib import Path
import pytest
import yaml

SETUP = Path(__file__).resolve().parents[2] / "setup"
ROOT = SETUP / "manifest"

_spec = importlib.util.spec_from_file_location("forge_environments", SETUP / "environments" / "load.py")
environments = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(environments)

# マニフェストはテンプレート。埋めた形で確かめる。埋める前の ${DOMAIN} を見ても、
# 実際に当たるものが正しいかは分からない。
ENVIRONMENT = environments.DEFAULT
VALUES = environments.values(ENVIRONMENT)


def documents(name):
    text = environments.render((ROOT / name).read_text(), ENVIRONMENT)
    return [item for item in yaml.safe_load_all(text) if item]


def first(name, kind):
    return next(item for item in documents(name) if item["kind"] == kind)


def test_app_is_production_and_probes_match_the_published_host():
    config = first("app.yaml", "ConfigMap")
    assert config["data"]["APP_ENV"] == "production"

    deployment = first("app.yaml", "Deployment")
    app = deployment["spec"]["template"]["spec"]["containers"][0]
    # 版はdigestで固定する。タグだと、同じ名前で中身が変わる。
    assert "@sha256:" in app["image"]
    assert app["securityContext"]["readOnlyRootFilesystem"] is True
    assert app["securityContext"]["capabilities"]["drop"] == ["ALL"]
    # 健全性の確認も公開している名前で行う。名前が違うとTrustedHostに弾かれる。
    for probe in ("readinessProbe", "livenessProbe", "startupProbe"):
        assert app[probe]["httpGet"]["httpHeaders"] == [
            {"name": "Host", "value": VALUES["DOMAIN"]}]

    service = first("app.yaml", "Service")
    assert service["spec"].get("type", "ClusterIP") == "ClusterIP"


def test_database_retains_claims_and_has_no_external_port():
    service = first("postgres.yaml", "Service")
    assert service["spec"].get("type", "ClusterIP") == "ClusterIP"

    stateful = first("postgres.yaml", "StatefulSet")
    # 消しても保存領域は残す。取り違えて消したときに、業務データを失わないため。
    assert stateful["spec"]["persistentVolumeClaimRetentionPolicy"] == {
        "whenDeleted": "Retain", "whenScaled": "Retain"}
    container = stateful["spec"]["template"]["spec"]["containers"][0]
    assert "POSTGRES_PASSWORD" not in {item["name"] for item in container["env"]}

    # 繋げるのは管理アプリと、印を付けたPodだけ。
    policy = first("postgres.yaml", "NetworkPolicy")
    allowed = policy["spec"]["ingress"][0]["from"]
    assert {"podSelector": {"matchLabels": {"app": "koyorina"}}} in allowed


def test_publication_is_separate_and_does_not_reuse_an_existing_certificate():
    proxy = first("gateway.yaml", "NginxProxy")
    assert proxy["spec"]["kubernetes"]["service"]["loadBalancerClass"] == "metallb.io/l2"
    assert proxy["spec"]["kubernetes"]["service"]["loadBalancerIP"] == VALUES["METALLB_IP"]

    certificate = first("gateway.yaml", "Certificate")
    gateway = first("gateway.yaml", "Gateway")
    listener = gateway["spec"]["listeners"][0]
    assert listener["hostname"] == VALUES["DOMAIN"]
    # 証明書はこのGateway専用。他で使っているSecretを指さない。
    assert certificate["spec"]["secretName"] == "koyorina-tls"
    assert listener["tls"]["certificateRefs"][0]["name"] == "koyorina-tls"

    route = first("gateway.yaml", "HTTPRoute")
    # 生成アプリは管理画面と別オリジン（<id>.DOMAIN / <id>-dev.DOMAIN）で配信する。
    wildcard = "*." + VALUES["DOMAIN"]
    assert certificate["spec"]["dnsNames"] == [VALUES["DOMAIN"], wildcard]
    assert [item["hostname"] for item in gateway["spec"]["listeners"]] == [VALUES["DOMAIN"], wildcard]
    assert route["spec"]["hostnames"] == [VALUES["DOMAIN"], wildcard]
    assert {ref["sectionName"] for ref in route["spec"]["parentRefs"]} == {"https", "https-apps"}


def test_every_built_image_is_pinned_where_make_pin_can_reach_it():
    """押し込んだのに古いまま動く、を二度と起こさない。

    イメージはdigestで固定しているので、pushしただけでは何も変わらない。
    マニフェストのdigestを差し替えて初めて当たる。その差し替えは make pin が
    やるが、pin が見ているのは setup/manifest/*.yaml だけ。

    実際に2回やられている。agent はマニフェストに無くクラスタの値を引き継いで
    いたため何度当てても古いまま、preview-runtime は Secret に手で入れる運用で
    どのスクリプトも更新していなかった。どちらも「成功したのに直っていない」
    という形で出るので、いちばん気づきにくい。

    Makefileが作るイメージと、pinが差し替えられる場所を突き合わせる。
    """
    makefile = (SETUP.parent / "Makefile").read_text(encoding="utf-8")
    services = next(line.split(":=")[1].split() for line in makefile.splitlines()
                    if line.startswith("ALL_SERVICES"))
    # イメージ名自体はAPP_NAME変数なので、ここではpin.pyが使う短い名前
    # （app/agent/preview-runtime）だけをMakefileのサービスと突き合わせる。
    assert set(services) == {"app", "agent", "preview-runtime"}, (
        "サービスが増えた。ここと pin.py へ足すこと。")

    pin = importlib.util.spec_from_file_location("forge_pin", ROOT / "pin.py")
    module = importlib.util.module_from_spec(pin)
    pin.loader.exec_module(module)
    assert set(module.REPOSITORIES) == set(services)

    for service in services:
        repository = module.TEMPLATE[service]
        assert module.pins(service), (
            f"{repository} をdigestで固定しているマニフェストがありません。"
            "マニフェストの外（Secretや引数）に置くと make pin が届かず、"
            "押し込んでも古いイメージのまま動き続けます。")
        # make pin のループにも入っていること。片方だけでは差し替わらない。
        assert service in makefile.split("for service in $(ALL_SERVICES)")[1].split("done")[0]
