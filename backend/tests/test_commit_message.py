"""生成コミットの件名。履歴を上から眺めて、何が起きた回かが分かること。

以前は依頼文をそのまま件名にしていた。依頼文は会話の一部なので、
実際に「コミットしておいてくれない？」という件名が履歴に残っていた。
"""
import pytest
from backend.domain.commit_message import compose, kind, summarize

CHANGED = [("M", "backend/main.py")]
ADDED = [("A", "frontend/src/components/DateFilter.vue"), ("M", "backend/routers/x.py")]


def subject(instruction, changes=CHANGED):
    return compose(instruction, changes).splitlines()[0]


def test_the_first_generation_starts_the_history():
    assert subject("", [("A", "backend/main.py")]) == "feat: 初回生成"


@pytest.mark.parametrize("instruction,prefix", [
    ("金額がずれるのを直してくれない？", "fix"),
    ("一覧でエラーになる", "fix"),
    ("保存できない不具合を修正", "fix"),
    ("ライブラリを最新に上げてください", "upd"),
    ("依存を更新して", "upd"),
    ("一覧に日付の絞り込みを追加してほしい", "feat"),
    ("CSV出力に対応して", "feat"),
    ("発行者名をコンボボックスにする", "chg"),
])
def test_the_kind_is_read_from_how_it_was_asked(instruction, prefix):
    """何をしたかったかは依頼文にある。ファイルの増減は結果でしかない。"""
    assert subject(instruction).startswith(prefix + ": ")


def test_a_new_file_means_a_feature_when_the_wording_does_not_say():
    """言い回しで決まらないときだけ、変わったファイルを見る。"""
    assert kind("一覧の並びを見直す", ADDED) == "feat"
    assert kind("一覧の並びを見直す", CHANGED) == "chg"


def test_the_verb_is_not_cut_in_half():
    """「直してくれない？」から頼み方だけを落とす。「直」まで削らない。

    サ変名詞（追加する）か動詞（直す）かを見分ける手は無いので、残すほうへ倒す。
    """
    assert summarize("金額がずれるのを直してくれない？") == "金額がずれるのを直して"
    assert summarize("CSV出力をお願いします。") == "CSV出力を"
    assert summarize("一覧を整える") == "一覧を整える"


def test_a_long_or_multiline_request_becomes_one_readable_line():
    """一覧は1行で並ぶ。長文をそのまま入れると表示が崩れる。"""
    line = subject("一覧を直して\n\nついでに色も変えて")
    assert "\n" not in line and len(line) <= 80


def test_the_body_says_what_changed_and_where_it_came_from():
    text = compose("CSV出力を追加して", ADDED, job_id="66d03be2", app_name="領収書管理")
    assert text.splitlines()[0] == "feat: CSV出力を追加して"
    assert text.splitlines()[1] == ""          # 件名と本文は空行で分ける
    assert "追加1・変更1（2ファイル）" in text
    assert "frontend/src/components/DateFilter.vue" in text
    assert "app: 領収書管理" in text and "job: 66d03be2" in text


def test_many_files_are_summarised_rather_than_listed():
    changes = [("M", f"backend/routers/{n}.py") for n in range(12)]
    text = compose("全体を整える", changes)
    assert "ほか7件" in text and "変更12（12ファイル）" in text


def test_an_empty_request_still_produces_a_usable_subject():
    """依頼文が空白だけでも、件名の無いコミットは作らない。"""
    assert subject("   ") == "feat: 初回生成"
    assert summarize("？？？") == "？？？"


MODEL_REPORT = """## 完了した作業の概要

発行者名をコンボボックスにしました。過去の登録から候補を出します。

COMMIT: feat: 領収書の発行者名を候補から選べるように
"""


def test_the_model_tells_us_what_it_changed():
    """何を変えたかはモデルが知っている。依頼文からの推測より正確。"""
    text = compose("発行者を選びやすくして", CHANGED, report=MODEL_REPORT)
    assert text.splitlines()[0] == "feat: 領収書の発行者名を候補から選べるように"


def test_the_request_is_used_when_the_model_says_nothing():
    """申告が無ければ、これまでどおり依頼文から組み立てる。"""
    assert subject("CSV出力を追加して") == "feat: CSV出力を追加して"
    assert compose("CSV出力を追加して", CHANGED, report="やりました。").splitlines()[0] \
        == "feat: CSV出力を追加して"


@pytest.mark.parametrize("line", [
    "COMMIT: 領収書の出力を追加",            # 種類が無い
    "COMMIT: wip: 途中まで",                 # 知らない種類
    "COMMIT: feat:",                         # 中身が無い
    "COMMIT feat: 記号ちがい",
])
def test_a_malformed_claim_is_ignored(line):
    """届くのは生成物と同じ信用しない文字列。形が違えば使わない。"""
    assert compose("CSV出力を追加して", CHANGED,
                   report=f"作業しました。\n{line}\n").splitlines()[0] == "feat: CSV出力を追加して"


def test_the_claim_never_breaks_the_commit_into_more_lines():
    """件名は1行。改行や制御文字を混ぜて本文を偽装させない。"""
    report = "COMMIT: fix: 一行目 二行目\n\n本文のふり\napp: にせもの\n"
    text = compose("直して", CHANGED, app_name="本物")
    assert text.count("app: ") == 1
    first = compose("直して", CHANGED, report=report, app_name="本物").splitlines()[0]
    assert first.startswith("fix: ") and "\n" not in first
    assert "にせもの" not in first


def test_a_long_claim_is_cut():
    report = "COMMIT: chg: " + "あ" * 200
    assert len(compose("整える", CHANGED, report=report).splitlines()[0]) <= 80


def test_the_last_claim_wins():
    """やり直したターンで2つ並ぶことがある。最後のものが最新。"""
    report = "COMMIT: chg: 古い\n途中経過\nCOMMIT: fix: 新しい\n"
    assert compose("直して", CHANGED, report=report).splitlines()[0] == "fix: 新しい"
