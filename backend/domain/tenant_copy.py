"""テナント移行のコピー。移行用Podの中で動かすスクリプトを組み立てる。

生成（codex-controller）とプレビュー（preview-controller）の両方の移行で使う。

- 移行元はリンクを辿らずに一度だけ歩き、コピーも検証もその一覧で行う。以前は
  コピー（shutil.copytree）はフォルダへのリンクを辿って実体を写し、検証（rglob）は
  辿らなかったため、フォルダへのリンクが1つあるだけで「copy verification failed」
  になっていた。
- リンクは、アプリの中を指すもの（相対パスで外へ出ないもの。backend/static ->
  ../frontend/dist など）だけをリンクのまま移す。外を指すもの（絶対パス、.. で出る
  もの）は別テナントの領域を指せるので、パスを添えて止める。
- 起動時に作り直せる物（依存の生成物、プレビューの仮想環境やキャッシュ）は移さない。
  プレビューの仮想環境はイメージのPythonを絶対パスで指すので、移すとここで止まる。
- 検証に落ちたら、食い違ったパス（名前だけ。中身は出さない）をログへ出す。
  移行中に誰かが書き込んだのか、コピーが欠けたのかを見分けるため。
"""
from __future__ import annotations

# 依存の導入で作り直せるもの。移しても容量と時間を食うだけ。
DERIVED = ("'.venv'", "'node_modules'", "'__pycache__'")

_RUNNER = r'''
import hashlib, os, shutil, sys
from pathlib import Path
DERIVED = frozenset({%(derived)s})


def contained(root, path):
    """リンク先がrootの中に収まるか。絶対パスと、.. でrootの外へ出るものは収まらない。"""
    target = os.readlink(path)
    if os.path.isabs(target):
        return False
    resolved = os.path.normpath(os.path.join(os.path.dirname(os.path.relpath(path, root)), target))
    return resolved != '..' and not resolved.startswith('..' + os.sep)


def entries(root, skip_derived, rebuilt=()):
    """rootより下のフォルダ・ファイル・リンク（相対パス）。リンクは辿らない。

    rebuilt はrootからの相対パス。そこから下は見ない（移さず、比べもしない）。
    """
    if root.is_symlink():
        raise RuntimeError('symlink is not migratable: ' + root.name)
    rebuilt = {Path(p) for p in rebuilt}
    folders, files, links = [], [], []
    for base, dirs, names in os.walk(root, followlinks=False):
        here = Path(base).relative_to(root)
        if skip_derived:
            dirs[:] = [d for d in dirs if d not in DERIVED]
        dirs[:] = [d for d in dirs if here / d not in rebuilt]
        names = [n for n in names if here / n not in rebuilt]
        for name in dirs + names:
            path = Path(base) / name
            if path.is_symlink():
                if not contained(root, path):
                    raise RuntimeError('symlink is not migratable: ' + str(here / name))
                links.append(here / name)
        dirs[:] = [d for d in dirs if not (Path(base) / d).is_symlink()]
        folders += [here / d for d in dirs]
        files += [here / n for n in names
                  if not (Path(base) / n).is_symlink() and (Path(base) / n).is_file()]
    return sorted(folders), sorted(files), sorted(links)


def sha(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        while chunk := stream.read(1024 * 1024):
            h.update(chunk)
    return h.hexdigest()


def fingerprint(root, files, links):
    """ファイルは中身、リンクはリンク先の文字列で比べる。"""
    result = {str(p): sha(root / p) for p in files}
    result.update({str(p): 'link:' + os.readlink(root / p) for p in links})
    return result


def verify(src, dst, expected, actual):
    """名前と中身が同じか。違えば、違うパスだけを出して止める。"""
    a = fingerprint(src, *expected)
    b = fingerprint(dst, *actual)
    if a != b:
        differ = sorted(k for k in a.keys() | b.keys() if a.get(k) != b.get(k))
        print('copy verification failed: %%d path(s) differ' %% len(differ), file=sys.stderr)
        for key in differ[:20]:
            state = 'missing' if key not in b else 'extra' if key not in a else 'changed'
            print('  ' + state + ': ' + key, file=sys.stderr)
        raise RuntimeError('copy verification failed')


for relative, skip_derived, rebuilt in ITEMS:
    src, dst = SOURCE / relative, TARGET / relative
    if not src.exists() and not src.is_symlink():
        continue
    if dst.exists() or dst.is_symlink():
        shutil.rmtree(dst) if dst.is_dir() and not dst.is_symlink() else dst.unlink()
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.is_file() and not src.is_symlink():
        shutil.copy2(src, dst)
        verify(src.parent, dst.parent, ([Path(src.name)], []), ([Path(dst.name)], []))
        continue
    folders, files, links = entries(src, skip_derived, rebuilt)
    dst.mkdir()
    for folder in folders:  # 空のフォルダも作る（gitの refs/tags など）
        (dst / folder).mkdir(parents=True, exist_ok=True)
    for name in files:
        shutil.copy2(src / name, dst / name)
    for name in links:  # アプリの中を指すリンクは、リンクのまま移す
        os.symlink(os.readlink(src / name), dst / name)
    verify(src, dst, (files, links), entries(dst, skip_derived, rebuilt)[1:])
'''


def copy_script(items: list[tuple], source: str = "/source", target: str = "/target") -> str:
    """items は (移行元からの相対パス, 依存の生成物を除くか[, 移さない相対パスの組])。

    パスは呼び出し側がUUIDから作る。
    """
    items = [(item[0], item[1], tuple(item[2]) if len(item) > 2 else ()) for item in items]
    header = f"from pathlib import Path\nSOURCE=Path({source!r})\nTARGET=Path({target!r})\nITEMS={items!r}\n"
    return header + _RUNNER % {"derived": ", ".join(DERIVED)}
