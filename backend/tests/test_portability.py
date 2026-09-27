"""別の環境へ持っていくとき、Pythonを書き換えずに済むこと。

クローンして別ドメインへ配備する前提。環境ごとに変わる値は設定から渡し、
マニフェストとシェルスクリプトの手直しだけで動く状態を保つ。
"""
from uuid import UUID
import pytest

OTHER = "registry.example.test"
DIGEST = "sha256:" + "a" * 64


def codex_settings(**overrides):
    from backend.worker.controller import ControllerSettings
    base = {"_env_file": None, "token": "c" * 40,
            "agent_image": f"registry.example.com/koyorina-agent@{DIGEST}"}
    return ControllerSettings(**{**base, **overrides})


def preview_settings(**overrides):
    from backend.worker.preview_controller import ControllerSettings
    base = {"_env_file": None, "token": "p" * 40,
            "runtime_image": f"registry.example.com/koyorina-preview-runtime@{DIGEST}"}
    return ControllerSettings(**{**base, **overrides})


# ---- 取得元のレジストリ -----------------------------------------------------

def test_the_agent_registry_can_be_moved_to_another_domain():
    settings = codex_settings(image_registry=OTHER,
                              agent_image=f"{OTHER}/koyorina-agent@{DIGEST}")
    assert settings.agent_image.startswith(OTHER)


def test_the_preview_registry_can_be_moved_to_another_domain():
    settings = preview_settings(image_registry=OTHER,
                                runtime_image=f"{OTHER}/koyorina-preview-runtime@{DIGEST}")
    assert settings.runtime_image.startswith(OTHER)


@pytest.mark.parametrize("image", [
    f"registry.attacker.test/koyorina-agent@{DIGEST}",   # 別の取得元
    "registry.example.test/koyorina-agent:latest",        # digest固定でない
    f"{OTHER}/something-else@{DIGEST}",                    # 別のリポジトリ
])
def test_an_image_outside_the_configured_registry_is_refused(image):
    """取得元を設定可能にしても、任意のイメージは起動させない。"""
    with pytest.raises(ValueError):
        codex_settings(image_registry=OTHER, agent_image=image)


def test_the_preview_runtime_is_pinned_to_a_digest_too():
    with pytest.raises(ValueError):
        preview_settings(image_registry=OTHER, runtime_image=f"{OTHER}/koyorina-preview-runtime:latest")


# ---- クラスタごとに変わる値 -------------------------------------------------

def agent_pod(**overrides):
    from backend.worker.controller import resources
    return resources(UUID("a5811e7e-a100-41d3-9100-0412e340ca68"), codex_settings(**overrides))


def test_the_generation_node_comes_from_configuration():
    """seccompプロファイルを置いたノードはクラスタごとに違う。"""
    spec = agent_pod(node="worker-9")["pods"]["spec"]
    assert spec["nodeSelector"]["kubernetes.io/hostname"] == "worker-9"


def test_the_storage_class_comes_from_configuration():
    """local-path は手元のk3sの都合。別のクラスタでは別の名前になる。"""
    claim = agent_pod(storage_class="standard-rwo")["persistentvolumeclaims"]
    assert claim["spec"]["storageClassName"] == "standard-rwo"


def test_the_preview_node_and_claim_come_from_configuration():
    from backend.worker.preview_controller import manifests
    from backend.domain.tenant_storage import preview_claim
    tenant_id = "11111111-1111-4111-8111-111111111111"
    shapes = manifests("a5811e7e-a100-41d3-9100-0412e340ca68", tenant_id,
                       preview_settings(node="worker-9", storage_class="standard-rwo"),
                       {"APP_ORIGIN": "https://forge.test"}, "2026-09-12T05:00:00+00:00")
    spec = shapes["deployments"]["spec"]["template"]["spec"]
    assert spec["nodeSelector"]["kubernetes.io/hostname"] == "worker-9"
    assert [v for v in spec["volumes"] if v.get("persistentVolumeClaim")][0
        ]["persistentVolumeClaim"]["claimName"] == preview_claim(tenant_id)


# ---- Pythonに環境固有値を残さない -------------------------------------------

def test_no_environment_specific_values_are_hardcoded_in_python():
    """設定で変えられない直書きが復活したら落とす。

    既定値としての登場は許す（設定で上書きできるため）。禁じるのは、
    設定を経由せずマニフェスト生成やバリデーションへ埋め込むこと。
    """
    import re
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    offenders = []
    for path in sorted(root.rglob("*.py")):
        if "/tests/" in path.as_posix() or "/conventions/" in path.as_posix():
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            # 正規表現リテラルの `\.` を取り逃がさないよう、エスケープを外してから探す。
            flat = line.replace("\\", "")
            if re.search(r"k3s-agent|local-path|192\.168\.", flat):
                # 既定値としての登場だけを許す。`Field(pattern=...)` のように
                # 検証へ埋め込む形は、設定で変えられないので許さない。
                default = re.search(r'^\s*\w+\s*:\s*[\w\[\]| ]+\s*=\s*'
                                    r'(?:"[^"]*"|\'[^\']*\'|Field\(\s*default\s*=\s*(?:"[^"]*"|\'[^\']*\'))',
                                    line)
                if default:
                    continue
                offenders.append(f"{path.relative_to(root.parent)}:{number} {line.strip()[:80]}")
    assert not offenders, "設定を経由しない環境固有値:\n" + "\n".join(offenders)


def test_agent_image_contains_the_generation_skills():
    root = __import__("pathlib").Path(__file__).resolve().parents[2]
    dockerfile = (root / "setup/manifest/Dockerfile.agent").read_text()
    assert "COPY Skills/ ./Skills/" in dockerfile
