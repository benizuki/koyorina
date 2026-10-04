"""生成アプリ用Registryの設定と参照。

公開機能はprivate/Artifact Registryの保存先を環境変数で選ぶ。
プレビューは引き続きソースを共有ランタイムで動かし、イメージを作らない。
脆弱性検査の有効状態はRegistry種別とは独立した運用設定として扱う。
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
    return f"{host}/{str(project_id)}:{str(job_id)}"


def describe(kind: str, host: str, *, scanning_enabled: bool = False) -> dict:
    """画面に出す説明。脆弱性検査の有無は運用の判断材料になるため含める。"""
    return {
        "kind": kind,
        "label": KINDS.get(kind, kind),
        "host": host,
        # 検査API・リポジトリ設定が有効な場合だけ、検査ありと表示する。
        "vulnerability_scanning": kind == ARTIFACT and scanning_enabled,
        "note": ("イメージはGCP側に保存されます。脆弱性検査にはAPIとリポジトリの設定が必要です。"
                 if kind == ARTIFACT else
                 "イメージはクラスタの中だけに置きます。脆弱性検査は付きません。"),
    }
