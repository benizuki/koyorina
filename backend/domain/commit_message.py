"""生成1回ぶんのコミット件名を、実際の変更から組み立てる。

以前は依頼文をそのまま件名にしていた。依頼文は会話の一部なので、
「コミットしておいてくれない？」のような、履歴として読めないものが並ぶ。

前置きで種類が分かるようにする。履歴を上から眺めたときに、
何が起きた回なのかが1文字目で見分けられる。

    feat:  機能追加        fix:  不具合の修正
    chg:   変更            upd:  更新（依存・版上げなど）

種類は依頼文の言い回しと、実際に変わったファイルの両方から決める。
言い回しだけだと「直して」と書かれた機能追加を取り違えるし、
ファイルだけだと追加と修正が区別できない。
"""
import re

FIX = ("直し", "直す", "直って", "修正", "不具合", "バグ", "エラー", "落ちる", "動かな",
       "おかしい", "できない", "失敗", "治", "fix", "bug")
# 「見直す」は直すではない。文字だけ見ると FIX に当たってしまうので、先に外す。
NOT_FIX = ("見直", "手直し")
UPDATE = ("更新", "アップデート", "バージョン", "最新", "上げ", "依存", "ライブラリ",
          "update", "upgrade", "bump")
FEATURE = ("追加", "足し", "足す", "新しく", "作って", "作成", "できるように", "対応",
           "導入", "実装", "付け", "増やし", "add", "feat")
# 件名は一覧で1行に収める。長い依頼文をそのまま入れると、表示が崩れて読めない。
MAX_SUBJECT = 72


def kind(instruction: str, changes) -> str:
    """前置きを決める。依頼文の言い回しを先に見て、無ければ変更の形で決める。

    依頼文のほうを優先するのは、そちらが「何をしたかったか」だから。
    ファイルの増減は結果でしかなく、機能追加でも既存ファイルの変更だけで
    済むことがある。
    """
    text = (instruction or "").strip().lower()
    if not text:
        return "feat"          # 初回生成。ここから始まる。
    for word in NOT_FIX:
        text = text.replace(word, "")
    if any(word in text for word in FIX):
        return "fix"
    if any(word in text for word in UPDATE):
        return "upd"
    if any(word in text for word in FEATURE):
        return "feat"
    # 言い回しで決まらないときだけ、変わったファイルを見る。
    return "feat" if any(change == "A" for change, _ in changes) else "chg"


def summarize(instruction: str) -> str:
    """依頼文から件名を作る。1行にして、頼み方の枕詞を落とす。"""
    line = (instruction or "").strip().splitlines()[0] if (instruction or "").strip() else ""
    line = re.sub(r"\s+", " ", line).strip()
    # 「〜してくれない？」「〜をお願いします」のような頼み方を落として、
    # 何をしたのかだけを残す。
    #
    # 「して」までは落とさない。「直してくれない？」の「して」は動詞の一部で、
    # 落とすと「直」になる。サ変名詞（追加する）か動詞（直す）かを見分ける手は
    # 無いので、残すほうへ倒す。「追加して」は少し口語だが、壊れはしない。
    line = re.sub(r"(ください|くれない|くれる|ほしい|欲しい|お願いします?|"
                  r"もらえますか|もらえる)[。．.!！?？]*$", "", line).strip()
    line = re.sub(r"[。．.]+$", "", line).strip()
    return line[:MAX_SUBJECT] or "生成"


KINDS = ("feat", "fix", "chg", "upd")
MARKER = re.compile(r"^\s*COMMIT:\s*(feat|fix|chg|upd)\s*[:：]\s*(.+?)\s*$",
                    re.IGNORECASE | re.MULTILINE)


def from_model(text: str) -> str:
    """生成モデルが最後に置いた1行を件名として受け取る。

    何を変えたかはモデルがいちばん知っている。依頼文からの推測より正確なので、
    あればこちらを使う。ただし**そのまま信じない**。届くのは生成物と同じ
    信用しない文字列なので、形と長さを確かめ、1行だけを取り出す。

    無い・壊れているときは空文字。呼び出し側が依頼文からの組み立てへ戻す。
    """
    found = MARKER.findall(text or "")
    if not found:
        return ""
    kind_word, summary = found[-1]          # 複数あれば最後のものを採る。
    summary = re.sub(r"[\x00-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]", " ", summary)
    summary = re.sub(r"\s+", " ", summary).strip().strip("`\"'")
    if not summary:
        return ""
    return f"{kind_word.lower()}: {summary[:MAX_SUBJECT]}"


def tally(changes) -> str:
    """変わったファイルの内訳。件名だけでは規模が分からない。"""
    labels = {"A": "追加", "M": "変更", "D": "削除", "R": "改名", "C": "複製"}
    counts = {}
    for change, _ in changes:
        counts[change] = counts.get(change, 0) + 1
    parts = [f"{labels.get(code, code)}{count}"
             for code, count in sorted(counts.items()) if count]
    return "・".join(parts)


def compose(instruction: str, changes, job_id="", app_name="", report="") -> str:
    """コミットの本文を組み立てる。件名・空行・内訳・出どころの順。

    件名はモデルの申告を優先する。何を変えたかはモデルが知っていて、
    依頼文からの推測より正確なため。申告が無ければ依頼文から組み立てる。
    """
    changes = [(str(change)[:1], str(path)) for change, path in changes]
    subject = from_model(report) or (
        f"{kind(instruction, changes)}: {summarize(instruction)}"
        if (instruction or "").strip() else "feat: 初回生成")
    lines = [subject, ""]
    if changes:
        shown = "、".join(path for _, path in changes[:5])
        if len(changes) > 5:
            shown += f" ほか{len(changes) - 5}件"
        lines.append(f"{tally(changes)}（{len(changes)}ファイル）")
        lines.append(shown)
        lines.append("")
    if app_name:
        lines.append(f"app: {app_name}")
    if job_id:
        lines.append(f"job: {job_id}")
    return "\n".join(lines).strip() + "\n"
