"""プレビューへ渡す利用者指定の環境変数。

生成アプリが外部のAPIキーなどを必要とすることがある。画面から足せるようにするが、
プラットフォームが決める値（DB接続先・転送の合言葉・配信元のパス）は書かせない。
上書きされると転送認証やベースパスが壊れる。

値の見せ方は項目ごとに選ぶ。
  secret=false … 画面にそのまま表示する（接続先URLや機能フラグなど）
  secret=true  … 画面へ返さない。入れ替えるときだけ新しい値を送る
"""
import re
from uuid import UUID
from backend.domain.preview import PACKAGE_SOURCE, runtime_environment

NAME = re.compile(r"[A-Z_][A-Z0-9_]{0,63}")
MAX_ENTRIES = 50
MAX_VALUE_BYTES = 4096
# 制御文字はマニフェストやログの見た目を壊す。改行も受け付けない。
CONTROL = re.compile(r"[\x00-\x1f\x7f]")

# プラットフォームが決めるキー。runtime_environment から引くので、
# 向こうにキーが増えてもこちらの守りが遅れない。
# 取得元は未設定でも予約しておく。あとから設定したときに、画面で先に同じ名前を
# 入れていた人のプレビューだけ別のレジストリを見る、という状態を作らない。
RESERVED = frozenset(runtime_environment(UUID(int=0), "", "", "", "", "")) | set(PACKAGE_SOURCE)


class InvalidEnvironment(ValueError):
    """画面へそのまま出せる日本語の理由を持つ。"""


def parse(entries) -> list[dict]:
    """画面から来た指定を検証して正規化する。値の保持はここでは決めない。"""
    if not isinstance(entries, list):
        raise InvalidEnvironment("環境変数の形式が不正です。")
    if len(entries) > MAX_ENTRIES:
        raise InvalidEnvironment(f"環境変数は{MAX_ENTRIES}件までです。")
    result, seen = [], set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise InvalidEnvironment("環境変数の形式が不正です。")
        name = str(entry.get("name", "")).strip()
        if not NAME.fullmatch(name):
            raise InvalidEnvironment(f"名前は英大文字・数字・下線で指定してください: {name[:40]}")
        if name in RESERVED:
            raise InvalidEnvironment(f"{name} はKoyorinaが設定する項目のため指定できません。")
        if name in seen:
            raise InvalidEnvironment(f"同じ名前が重複しています: {name}")
        seen.add(name)
        secret = bool(entry.get("secret"))
        value = entry.get("value")
        if value is not None:
            value = str(value)
            if CONTROL.search(value):
                raise InvalidEnvironment(f"{name} の値に使えない文字が含まれています。")
            if len(value.encode()) > MAX_VALUE_BYTES:
                raise InvalidEnvironment(f"{name} の値が長すぎます。")
        elif not secret:
            raise InvalidEnvironment(f"{name} の値を入力してください。")
        result.append({"name": name, "value": value, "secret": secret})
    return result


def merge(stored, incoming) -> list[dict]:
    """保存済みと画面からの指定を突き合わせる。

    秘密の項目は画面へ返していないので、値が省かれていたら前の値を残す。
    ここで前の値を拾わないと、別の項目を直しただけで秘密が消える。
    """
    previous = {item["name"]: item for item in (stored or []) if isinstance(item, dict)}
    result = []
    for entry in incoming:
        value = entry["value"]
        if value is None:
            kept = previous.get(entry["name"], {})
            if not kept.get("secret") or kept.get("value") is None:
                raise InvalidEnvironment(f"{entry['name']} の値を入力してください。")
            value = kept["value"]
        result.append({"name": entry["name"], "value": value, "secret": entry["secret"]})
    return result


def visible(stored) -> list[dict]:
    """画面へ返す形。秘密の項目は値を出さず、設定済みかどうかだけ伝える。"""
    result = []
    for item in (stored or []):
        if not isinstance(item, dict):
            continue
        if item.get("secret"):
            result.append({"name": item["name"], "secret": True, "value": None,
                           "configured": bool(item.get("value"))})
        else:
            result.append({"name": item["name"], "secret": False,
                           "value": str(item.get("value", "")), "configured": True})
    return result


def as_environment(stored) -> dict:
    """実行環境へ渡す形。予約名は最後にプラットフォーム側が上書きする。"""
    return {item["name"]: str(item["value"]) for item in (stored or [])
            if isinstance(item, dict) and item.get("name") not in RESERVED
            and item.get("value") is not None}


def names(stored) -> list[str]:
    """監査や記録に残す用。値は決して残さない。"""
    return sorted(item["name"] for item in (stored or []) if isinstance(item, dict) and item.get("name"))
