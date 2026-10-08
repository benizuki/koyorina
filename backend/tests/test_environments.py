"""環境ごとに変わる値が1か所に集まっていること。

ドメイン・イメージの置き場・GCPプロジェクトは、マニフェスト・Makefile・配備スクリプト・
テストの4か所から要る。同じ値をそれぞれに書き写すと、片方だけ直した状態が必ず生まれる。
実際、本番テナントへ持っていったときに、直した場所と直し忘れた場所が混ざった。
"""
import importlib.util
import re
import subprocess
from pathlib import Path
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
MANIFESTS = ROOT / "setup/manifest"
TEMPLATES = sorted(MANIFESTS.glob("*.yaml"))

_spec = importlib.util.spec_from_file_location(
    "forge_environments", ROOT / "setup/environments/load.py")
environments = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(environments)

NAMES = environments.names()
# 開発環境の値。ここを変えるときは、手元のk3sも一緒に変わる。
DEV = environments.values("dev")


def test_dev_environment_exists_and_is_the_default():
    """devは開発用k3sが使う唯一必須の環境。prod（本番固有の値）は必須にしない
    ——本番はAnsible経由ならgroup_varsからansible.envが生成され、そちらを使う。
    prodを手で持つかどうかは運用者の任意（setup/environments/example.envを
    コピーして作る）。"""
    assert "dev" in set(NAMES)
    assert environments.DEFAULT == "dev"


@pytest.mark.parametrize("name", NAMES)
def test_every_environment_defines_the_same_keys(name):
    """片方にしかない鍵があると、もう片方で埋め残しが出る。"""
    assert set(environments.values(name)) == set(DEV)


@pytest.mark.parametrize("name", NAMES)
def test_the_values_used_by_every_deployment_are_filled_in(name):
    """マニフェストとMakefileが使う値は、どの環境でも空にできない。"""
    values = environments.values(name)
    for key in ("DOMAIN", "REGISTRY", "GCP_PROJECT"):
        assert values[key], f"{name}.env の {key}"
    # AGENT_NODEはノード固定型ストレージ(RWO)のときだけ要る。RWX
    # (NODE_SELECTOR指定、またはStorage_ACCESS_MODE=ReadWriteMany、例:
    # Ceph/Filestore)ならどちらも空でよい。
    assert values["AGENT_NODE"] or values["NODE_SELECTOR"] or values["STORAGE_ACCESS_MODE"] == "ReadWriteMany", (
        f"{name}.env の AGENT_NODE（またはNODE_SELECTOR/RWX指定）")
    assert "/" not in values["DOMAIN"] and "://" not in values["DOMAIN"]


@pytest.mark.parametrize("name", NAMES)
def test_every_manifest_renders_without_leftovers(name):
    """${DOMAIN} のまま当てると、起動はするのに誰も繋がらない状態になる。"""
    values = environments.values(name)
    for path in sorted(MANIFESTS.glob("*.yaml")):
        text = environments.render(path.read_text(), name)
        assert "${" not in text, path.name
        assert [document for document in yaml.safe_load_all(text) if document], path.name
    gateway = environments.render((MANIFESTS / "gateway.yaml").read_text(), name)
    route = next(d for d in yaml.safe_load_all(gateway) if d and d["kind"] == "HTTPRoute")
    # 生成アプリは管理画面と別オリジン（*.DOMAIN）で配信する。
    assert route["spec"]["hostnames"] == [values["DOMAIN"], "*." + values["DOMAIN"]]


def test_the_templates_are_readable_as_yaml_before_rendering():
    """埋める前でも素のYAMLとして読めること。読めないとリンタもエディタも扱えない。"""
    for path in TEMPLATES:
        assert list(yaml.safe_load_all(path.read_text())), path.name


def test_the_gce_ingress_takes_its_host_from_the_chart_domain():
    """公開の入口だけ手で書き換える運用に戻すと、そこだけ古いドメインを指し続ける。

    GCEのIngressはHelm Chart(setup/helm/koyorina)が持つ。hostはvaluesのdomainから
    作り、どの環境のドメインも直書きしない。"""
    text = (ROOT / "setup/helm/koyorina/templates/ingress.yaml").read_text()
    assert '{{- range list .Values.domain (printf "*.%s" .Values.domain) }}' in text
    assert "host: {{ . | quote }}" in text
    for name in NAMES:
        domain = environments.values(name)["DOMAIN"]
        assert domain not in text, f"{name}.env の DOMAIN が直接書かれている"


@pytest.mark.parametrize("path", TEMPLATES, ids=lambda p: p.name)
def test_no_environment_specific_value_is_written_into_a_manifest(path):
    """書き写しが1つでも残っていると、そこだけ古い環境を指し続ける。"""
    text = path.read_text()
    for name in NAMES:
        for key, value in environments.values(name).items():
            if not value or key in {"GCP_REGION", "STORAGE_ACCESS_MODE"}:
                # リージョンはイメージの置き場の一部として現れるだけ。
                # STORAGE_ACCESS_MODEはReadWriteOnce/ReadWriteManyという固定の
                # 語彙で、postgres.yaml等は常にRWO（環境で変えない値）を直書きする。
                continue
            # 短い値は判定できない。"2" や "2Gi" は digest やサイズ指定の一部として
            # どのマニフェストにも現れる。守りたいのはドメインや置き場のような、
            # 書き写すと環境を取り違える値なので、そちらだけを見る。
            if len(value) < 8:
                continue
            assert value not in text, f"{path.name} に {name}.env の {key} が直接書かれている"


def test_the_makefile_takes_the_registry_from_the_environment():
    """push先を書き写すと、ENVを切り替えても古い置き場へ出てしまう。"""
    makefile = (ROOT / "Makefile").read_text()
    assert "setup/environments/load.py" in makefile
    for name in NAMES:
        expected = environments.values(name)["IMAGE_ROOT"]
        if not expected:
            continue
        listing = subprocess.run(["make", "images", f"ENV={name}"], cwd=ROOT,
                                 capture_output=True, text=True, check=True).stdout
        assert listing.strip()
        for line in listing.strip().splitlines():
            assert line.startswith(expected + "/"), line


def test_terraform_requires_project_and_domain_with_no_company_default():
    """OSS化により project_id/domain は必須変数。自社固有の既定値を持たせない。"""
    variables = (ROOT / "setup/gcp/terraform/variables.tf").read_text()
    for name in ("project_id", "domain"):
        block = re.search(r'variable "%s" \{(.+?)\n\}' % name, variables, re.S)
        assert block, f"{name}変数が見つからない"
        assert "default" not in block[1], f"{name}に既定値が残っている"


def test_the_renderer_refuses_a_value_it_does_not_know():
    with pytest.raises(SystemExit):
        environments.render("host: ${NOT_DEFINED_ANYWHERE}", "dev")


def test_reading_an_empty_value_stops_instead_of_deploying_nothing(monkeypatch):
    """空のまま配備すると、名前の無いホストや置き場を指しに行く。"""
    monkeypatch.setattr(environments, "values", lambda _name: {"IMAGE_ROOT": ""})
    with pytest.raises(SystemExit):
        environments.required("dev", "IMAGE_ROOT")
