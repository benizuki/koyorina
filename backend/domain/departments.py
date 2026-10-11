"""所属組織の階層。上位へたどる・配下を集める・利用許可が届くかを決める。

組織の数は多くても数百なので、毎回まとめて読み込んで辞書でたどる。
親の付け替えで輪ができないことは保存時に確かめるが、壊れたデータでも止まらないよう
たどる処理は訪れた組織を覚えておく。
"""
from collections.abc import Iterable, Mapping

# 種別。並びは画面の選択肢の順。
KINDS = {"division": "部門", "section": "課", "team": "チーム"}


def by_id(departments: Iterable) -> dict:
    return {department.id: department for department in departments}


def ancestors(table: Mapping, department_id: str | None) -> list:
    """自分自身から最上位までの組織。自分が先頭。"""
    chain, seen = [], set()
    current = table.get(department_id) if department_id else None
    while current is not None and current.id not in seen:
        seen.add(current.id)
        chain.append(current)
        current = table.get(current.parent_id) if current.parent_id else None
    return chain


def descendant_ids(table: Mapping, department_id: str) -> set[str]:
    """配下の組織のID（自分は含めない）。"""
    children: dict[str, list[str]] = {}
    for department in table.values():
        if department.parent_id:
            children.setdefault(department.parent_id, []).append(department.id)
    found, pending = set(), list(children.get(department_id, []))
    while pending:
        current = pending.pop()
        if current in found or current == department_id:
            continue
        found.add(current)
        pending.extend(children.get(current, []))
    return found


def usable(table: Mapping, department_id: str) -> bool:
    """自分と上位がすべて使う状態か。停止した組織は、その配下ごと選べなくする。"""
    chain = ancestors(table, department_id)
    return bool(chain) and all(department.enabled for department in chain)


def path_name(table: Mapping, department_id: str, separator: str = " / ") -> str:
    return separator.join(department.name for department in reversed(ancestors(table, department_id)))


def granted(table: Mapping, department_id: str | None, granted_ids: set[str]) -> bool:
    """所属組織か、その上位のどれかに許可があるか。

    下から上へたどり、停止中の組織に当たったらそこで止める。停止した組織の配下には、
    それより上に付けた許可も届かない（組織ごと止めた、という意味を保つ）。
    """
    for department in ancestors(table, department_id):
        if not department.enabled:
            return False
        if department.id in granted_ids:
            return True
    return False


def tree_order(table: Mapping) -> list:
    """画面に並べる順。親の直後に子が来る。同じ階層では並び順、次に名前。"""
    children: dict[str | None, list] = {}
    for department in table.values():
        parent = department.parent_id if department.parent_id in table else None
        children.setdefault(parent, []).append(department)
    for items in children.values():
        items.sort(key=lambda d: (d.sort_order, d.name))
    ordered, seen = [], set()

    def walk(parent):
        for department in children.get(parent, []):
            if department.id in seen:
                continue
            seen.add(department.id)
            ordered.append(department)
            walk(department.id)
    walk(None)
    # 輪になった壊れたデータも一覧から消えないよう、残りを最後に足す。
    ordered.extend(d for d in table.values() if d.id not in seen)
    return ordered
