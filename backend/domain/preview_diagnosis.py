"""プレビューが起動しなかった理由を、ログを読まない人の言葉へ言い換える。

実行ログは生成アプリの出力であり、指示としては扱わない。ここでやるのは形を見た分類と、
根拠になった1行の抜き出しだけ。抜き出した行は利用者自身のアプリの出力で、画面では
テキストとして表示する。

「ログを確認してください」と書くだけでは、読めない人はそこで詰まる。何が起きたかと、
次に何を頼めばよいかまで書く。
"""
import re

MAX_EVIDENCE = 300
# どの規則にも当たらなかったときに、根拠として拾う行の目印。
ERROR_LINE = re.compile(r"(Error|Exception|Traceback|Fatal|failed|FAILED|panic)")


def _module(match):
    name = match["name"]
    return (f"アプリが使う部品「{name}」が入っていないため、起動できませんでした。",
            f"チャットで「{name} を依存関係に追加して」と依頼してください。")


def _node_module(match):
    name = match["name"]
    return (f"画面側が使う部品「{name}」が見つからないため、起動できませんでした。",
            f"チャットで「{name} を package.json に追加して」と依頼してください。")


def _syntax(match):
    return ("アプリのコードに文法の誤りがあるため、起動できませんでした。",
            "チャットで「起動時の文法エラーを直して」と依頼してください。下の行も貼ると早く直ります。")


def _import(match):
    return (f"「{match['source']}」に「{match['name']}」が無いため、起動できませんでした。",
            "コードの一部が書きかけの可能性があります。チャットで「起動時の読み込みエラーを直して」と依頼してください。")


def _table(match):
    return (f"データベースに表「{match['name']}」がありません。",
            "初期化されていない可能性があります。チャットで「起動時にテーブルを作成するようにして」と依頼してください。")


def _column(match):
    return (f"データベースの項目「{match['name']}」がありません。",
            "仕様を変えた後に作業ディスクが古いままの可能性があります。"
            "「作業ディスクを削除」してから、もう一度起動してください。")


def _port(_match):
    return ("使用中のポートへ接続しようとして起動できませんでした。",
            "「停止」してから、もう一度「最新のコードで起動」を押してください。")


def _memory(_match):
    return ("メモリ不足で強制終了しました。",
            "一度に扱うデータを減らせないか、チャットで相談してください。続く場合は管理者に連絡してください。")


def _build(_match):
    return ("画面側のビルドに失敗したため、起動できませんでした。",
            "チャットで「画面のビルドエラーを直して」と依頼してください。下の行も貼ると早く直ります。")


def _permission(_match):
    return ("書き込みできない場所へ保存しようとして停止しました。",
            "チャットで「データの保存先を作業ディレクトリ内にして」と依頼してください。")


def _settings(_match):
    return ("設定値が足りないか、形式が合わないため起動できませんでした。",
            "「環境変数」を開き、アプリが必要とする項目が入っているか確認してください。")


def _env(match):
    return (f"環境変数「{match['name']}」が設定されていないため、起動できませんでした。",
            f"「環境変数」を開き、{match['name']} を追加して保存してください。")


# 上から順に見て、最初に当たったものを採用する。具体的な原因を先に置く。
RULES = (
    (re.compile(r"ModuleNotFoundError: No module named ['\"](?P<name>[^'\"]{1,80})['\"]"), _module),
    (re.compile(r"Cannot find module ['\"](?P<name>[^'\"]{1,120})['\"]"), _node_module),
    (re.compile(r"ImportError: cannot import name ['\"](?P<name>[^'\"]{1,80})['\"]"
                r" from ['\"](?P<source>[^'\"]{1,120})['\"]"), _import),
    (re.compile(r"(?:SyntaxError|IndentationError): [^\n]{0,160}"), _syntax),
    (re.compile(r"no such table:? (?P<name>[\w.\-]{1,80})"), _table),
    (re.compile(r"no such column:? (?P<name>[\w.\-]{1,80})"), _column),
    (re.compile(r"[Aa]ddress already in use|error while attempting to bind on address"), _port),
    (re.compile(r"OOMKilled|exit code 137|Out of memory|MemoryError"), _memory),
    (re.compile(r"npm ERR!|vite build failed|Build failed with \d+ error|esbuild.*[Ee]rror"), _build),
    (re.compile(r"PermissionError|Read-only file system|EACCES"), _permission),
    (re.compile(r"\d+ validation error|pydantic.*ValidationError"), _settings),
    (re.compile(r"KeyError: ['\"](?P<name>[A-Z][A-Z0-9_]{2,60})['\"]"), _env),
)


def _line(text: str, index: int) -> str:
    start = text.rfind("\n", 0, index) + 1
    end = text.find("\n", index)
    return text[start:end if end != -1 else len(text)].strip()[:MAX_EVIDENCE]


def _last_error_line(text: str) -> str:
    for line in reversed(text.splitlines()):
        stripped = line.strip()
        if stripped and ERROR_LINE.search(stripped):
            return stripped[:MAX_EVIDENCE]
    return ""


def diagnose(logs: str) -> dict:
    """失敗の理由・次にやること・根拠の1行を返す。分からなければ分からないと書く。"""
    text = (logs or "").strip()
    if not text:
        return {"message": "プレビューが起動できませんでした。実行ログは残っていません。",
                "hint": "「最新のコードで起動」でもう一度試してください。"
                        "それでも起動しない場合は管理者に連絡してください。",
                "evidence": None}
    for pattern, build in RULES:
        # 同じ失敗を繰り返していることがある。直近の1件を根拠にする。
        matches = list(pattern.finditer(text))
        if matches:
            message, hint = build(matches[-1])
            return {"message": message, "hint": hint, "evidence": _line(text, matches[-1].start())}
    line = _last_error_line(text)
    if line:
        return {"message": "アプリの起動時にエラーが発生しました。",
                "hint": "下の行をそのままチャットへ貼って、修正を依頼してください。",
                "evidence": line}
    return {"message": "プレビューが起動しないまま停止しました。",
            "hint": "「ログを見る」で出力を確認し、チャットで修正を依頼してください。",
            "evidence": None}
