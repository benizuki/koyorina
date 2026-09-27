"""生成アプリのイメージをどこへ置くか。

置き場は2つある。開発のように外部サービスを使わない構成と、GCPの
Artifact Registryを使う構成。後者は脆弱性検査が付くので、本番向き。
どちらを使うかは環境変数で選ぶ。経路の違いをアプリの他の部分へ持ち込まない。

**まだ使っていない。** 生成アプリのイメージ化が未実装のため、`reference` と
`describe` を呼ぶ処理は無い。プレビューはソースを展開して共有ランタイムで動かす方式で、
イメージを作らない（private/docs/preview-runtime.md）。

残してあるのは、本番でArtifact Registryを使うことが決まっているため。
`validate` だけは設定の検証で今も効いていて、誤った置き場の指定を起動時に弾く。
"""
import re

PRIVATE = "private"
ARTIFACT = "artifact"
KINDS = {
    PRIVATE: "クラスタ内のDocker Registry",
    ARTIFACT: "Artifact Registry（GCP）",
}
# <region>-docker.pkg.dev/<project>/<repository>
ARTIFACT_HOST = re.compile(
    r"^[a-z0-9-]+-docker\.pkg\.dev/[a-z][a-z0-9-]{4,28}[a-z0-9]/[a-z][a-z0-9-]{0,62}$")
# クラスタ内のサービス名か、ローカル検証のループバックだけ許す。
PRIVATE_HOST = re.compile(r"^(?:[a-z0-9-]+(?:\.[a-z0-9-]+)*(?::\d{2,5})?|127\.0\.0\.1:\d{2,5})$")


def validate(kind: str, host: str) -> None:
    if kind not in KINDS:
        raise ValueError("イメージの置き場は private か artifact を指定してください。")
    if not host:
        raise ValueError("イメージの置き場のホストを指定してください。")
    pattern = ARTIFACT_HOST if kind == ARTIFACT else PRIVATE_HOST
    if not pattern.fullmatch(host):
        raise ValueError("イメージの置き場の指定が不正です。")


def reference(kind: str, host: str, project_id, job_id) -> str:
    """1つの生成物に1つのタグ。同じタグを上書きしない。戻せなくなるため。"""
    validate(kind, host)
    return f"{host}/{str(project_id)}:{str(job_id)[:12]}"


def describe(kind: str, host: str) -> dict:
    """画面に出す説明。脆弱性検査の有無は運用の判断材料になるため含める。"""
    return {
        "kind": kind,
        "label": KINDS.get(kind, kind),
        "host": host,
        # Artifact Registryは取り込んだイメージを自動で走査する。自前のRegistryは付かない。
        "vulnerability_scanning": kind == ARTIFACT,
        "note": ("Artifact Registryが脆弱性検査を行います。イメージはGCP側に保存されます。"
                 if kind == ARTIFACT else
                 "イメージはクラスタの中だけに置きます。脆弱性検査は付きません。"),
    }
