"""生成モデルへ渡す唯一の能力。ワークスペースの外へは一切届かせない。

Codexでは権限プロファイル（`:root"="deny"`, network無効）がこの役割を担う。
自前のエージェントではプロファイルが無いため、ここが境界そのものになる。
コマンド実行や通信の手段は渡さない。読み書きも成果物として許される形式に限る。
"""
import ast
import json
import os
from pathlib import Path
from backend.domain.generation import (PLATFORM_FILES, REQUIRED_FILES, is_generated_leftover,
                                       artifact_path_is_allowed, parse_json_source,
                                       runtime_contract_problems)

MAX_FILE_BYTES = 200_000
MAX_LISTED = 200


def resolved(root: Path, path: str) -> Path:
    """ワークスペース配下の通常ファイルだけを指す絶対パスへ直す。"""
    if not artifact_path_is_allowed(path):
        raise ValueError("扱えないパスです。プロジェクト内の対応した拡張子のファイルを指定してください。")
    base = root.resolve(strict=True)
    target = (base / path).resolve()
    if not target.is_relative_to(base):
        raise ValueError("プロジェクトの外は参照できません。")
    if target.is_symlink():
        raise ValueError("シンボリックリンクは扱えません。")
    return target


def write_source(root: Path, path: str, text: str) -> dict:
    if len(text.encode()) > MAX_FILE_BYTES:
        return {"status": "rejected", "reason": "1ファイルの上限を超えています。"}
    try:
        target = resolved(root, path)
    except ValueError as exc:
        return {"status": "rejected", "reason": str(exc)}
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return {"status": "written", "path": path, "bytes": len(text.encode())}


def read_source(root: Path, path: str) -> dict:
    try:
        target = resolved(root, path)
    except ValueError as exc:
        return {"status": "rejected", "reason": str(exc)}
    if not target.is_file():
        return {"status": "missing", "path": path}
    data = target.read_bytes()[:MAX_FILE_BYTES]
    try:
        return {"status": "read", "path": path, "text": data.decode("utf-8")}
    except UnicodeDecodeError:
        return {"status": "rejected", "reason": "テキストとして読めないファイルです。"}


def list_sources(root: Path) -> dict:
    base = root.resolve(strict=True)
    found = []
    for directory, dirnames, filenames in os.walk(base, followlinks=False):
        dirnames[:] = [name for name in dirnames if not is_generated_leftover([name])]
        for name in sorted(filenames):
            relative = (Path(directory) / name).relative_to(base).as_posix()
            if (relative in PLATFORM_FILES or is_generated_leftover(relative.split("/"))
                    or not artifact_path_is_allowed(relative)):
                continue
            found.append(relative)
            if len(found) >= MAX_LISTED:
                return {"status": "listed", "files": sorted(found), "truncated": True}
    return {"status": "listed", "files": sorted(found), "truncated": False}


MAX_COLLECTED_BYTES = 20 * 1024 * 1024


def collect_sources(root: Path) -> dict:
    """ダウンロード用に、ファイルタブに出るものを中身ごと集める。一覧と同じ基準で選ぶ。

    一覧の200件の上限は画面のためのもので、ここでは掛けない。代わりに合計の大きさで止める。
    """
    base = root.resolve(strict=True)
    found, total = [], 0
    for directory, dirnames, filenames in os.walk(base, followlinks=False):
        dirnames[:] = sorted(name for name in dirnames if not is_generated_leftover([name]))
        for name in sorted(filenames):
            path = Path(directory) / name
            relative = path.relative_to(base).as_posix()
            if (relative in PLATFORM_FILES or is_generated_leftover(relative.split("/"))
                    or not artifact_path_is_allowed(relative) or path.is_symlink() or not path.is_file()):
                continue
            try:
                text = path.read_bytes().decode("utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            total += len(text.encode())
            if total > MAX_COLLECTED_BYTES:
                return {"status": "collected", "files": found, "truncated": True}
            found.append({"path": relative, "content": text})
    return {"status": "collected", "files": found, "truncated": False}


def check_sources(root: Path) -> dict:
    """成果物として受け取れる状態かを調べる。Koyorina側の検証と同じ観点で見る。

    自前のエージェントにはコマンド実行の手段が無く、書いたコードを自分で動かせない。
    構文の誤りや必須ファイルの不足を、生成が終わってから失敗として返すのではなく、
    作っている途中で気付けるようにする。
    """
    base = root.resolve(strict=True)
    problems, present, sources = [], set(), {}
    for directory, dirnames, filenames in os.walk(base, followlinks=False):
        dirnames[:] = [name for name in dirnames if not is_generated_leftover([name])]
        for name in sorted(filenames):
            relative = (Path(directory) / name).relative_to(base).as_posix()
            # 派生物は「直せ」と言わない。試験を走らせれば必ず出るもので、
            # 消してもまた出る。基盤が落とすので、無いものとして扱う。
            if relative in PLATFORM_FILES or is_generated_leftover(relative.split("/")):
                continue
            if not artifact_path_is_allowed(relative):
                problems.append(f"{relative}: 受け取れないファイル名・拡張子です。削除するか名前を変えてください。")
                continue
            present.add(relative.casefold())
            target = Path(directory) / name
            if target.is_symlink() or not target.is_file():
                problems.append(f"{relative}: 通常のファイルではありません。")
                continue
            try:
                text = target.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                problems.append(f"{relative}: UTF-8のテキストとして読めません。")
                continue
            sources[relative.casefold()] = text
            if len(text.encode()) > MAX_FILE_BYTES:
                problems.append(f"{relative}: 1ファイルの上限（{MAX_FILE_BYTES}バイト）を超えています。分割してください。")
            if relative.endswith(".py"):
                try:
                    ast.parse(text)
                except SyntaxError as exc:
                    problems.append(f"{relative}:{exc.lineno}: Pythonの構文エラー: {exc.msg}")
            elif relative.endswith(".json"):
                try:
                    parse_json_source(relative, text)
                except json.JSONDecodeError as exc:
                    problems.append(f"{relative}:{exc.lineno}: JSONの構文エラー: {exc.msg}")
    for required in REQUIRED_FILES:
        if required not in present:
            problems.append(f"{required}: 必須ファイルがありません。")
    if set(REQUIRED_FILES).issubset(present):
        problems.extend(runtime_contract_problems(sources))
    problems = sorted(problems)[:40]
    return {"status": "ok" if not problems else "problems", "problems": problems}
