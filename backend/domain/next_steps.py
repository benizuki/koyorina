"""初回に見送った機能を、次に頼めることとして受け取る。

利用者はコードを読めない。初回から全部を作らせると、出来上がるまで何も
確かめられず、違っていたときの手戻りが大きい。台帳が動くところまでを先に
出して、実際に触ってもらってから足していく。

そのために、何を見送ったかをモデルに申告させる。申告が無いと「足りない」の
ではなく「そういう仕様なのか」と受け取られてしまう。追加で問い合わせはしない。
いま終わったばかりのターンの最後に書かせる（件名と同じやり方）。

    NEXT: ユーザー管理画面を追加する
"""
import re

MARKER = re.compile(r"^\s*NEXT\s*[:：]\s*(.+?)\s*$", re.MULTILINE)
# 画面に並べる数。多すぎると、どれから頼めばよいのか分からなくなる。
MAX_ITEMS = 6
MAX_LENGTH = 80


def from_model(text: str) -> list[str]:
    """最後の発言から拾う。重複と空は落とし、長すぎるものは切る。"""
    found = []
    for line in MARKER.findall(text or ""):
        item = " ".join(line.split())
        if not item or item in found:
            continue
        found.append(item[:MAX_LENGTH])
        if len(found) >= MAX_ITEMS:
            break
    return found


def summary(steps: list[str]) -> str:
    """画面へ出す1行。見送ったものが無ければ空を返す（無いのに枠を出さない）。"""
    if not steps:
        return ""
    return ("今回は台帳が動くところまでを作りました。次はこのあたりを頼めます："
            + "／".join(steps))
