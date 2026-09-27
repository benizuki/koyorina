"""CSV / Excelの見本から列構成を推定する。元データは保存しない。"""
from __future__ import annotations

import csv
from datetime import datetime
import io
import re
from pathlib import PurePosixPath
import unicodedata
import xml.etree.ElementTree as ET
import zipfile

from backend.domain.projects import SampleColumn, SampleDataProfile


MAX_SAMPLE_BYTES = 5 * 1024 * 1024
MAX_SAMPLE_ROWS = 500
MAX_SAMPLE_COLUMNS = 100
MAX_XLSX_UNCOMPRESSED_BYTES = 25 * 1024 * 1024

ROLE_WORDS = {
    "timestamp": ("日時", "日付時刻", "時刻", "タイムスタンプ", "datetime", "timestamp", "time",
                  # 伝票の日付。「売上日」を金額と取り違えないよう、語全体で持つ。
                  "日付", "年月日", "売上日", "受注日", "発注日", "請求日", "入金日", "登録日",
                  "作成日", "記録日", "date"),
    "equipment": ("設備", "機械", "号機", "ライン", "machine", "equipment", "line"),
    "product": ("製品", "品番", "品名", "型式", "product", "item", "partnumber"),
    "lot": ("ロット", "lot", "batch"),
    "quantity": ("数量", "生産数", "出来高", "個数", "count", "quantity", "qty"),
    "result": ("判定", "良否", "合否", "result", "judgement", "judgment"),
    "good_count": ("良品数", "合格数", "ok数", "goodcount", "okcount"),
    "defect_count": ("不良数", "ng数", "defectcount", "rejectcount", "ngcount"),
    "defect_category": ("不良内容", "不良区分", "不良項目", "不良理由", "defect", "rejectreason"),
    "status": ("状態", "稼働状態", "ステータス", "status", "state"),
    "start_time": ("開始日時", "開始時刻", "計画開始", "予定開始", "実績開始", "開始",
                   "starttime", "startedat", "plannedstart", "actualstart"),
    "end_time": ("終了日時", "終了時刻", "計画終了", "予定終了", "実績終了", "終了",
                 "endtime", "endedat", "plannedend", "actualend"),
    "target": ("目標", "計画数", "予定数", "target", "planquantity"),
    "quality_value": ("測定値", "実測値", "特性値", "measurement", "measuredvalue"),
    "quality_item": ("測定項目", "検査項目", "品質項目", "characteristic", "qualityitem"),
    "specification_upper": ("上限", "上限値", "usl", "upperlimit"),
    "specification_lower": ("下限", "下限値", "lsl", "lowerlimit"),
    "category": ("分類", "区分", "カテゴリ", "category"),
    "value": ("値", "実績", "value"),
    "location": ("場所", "拠点", "事業所", "倉庫", "保管場所", "所在地", "location", "site", "warehouse"),
    "department": ("部署", "部門", "所属", "department", "division"),
    "person": ("担当者", "担当", "作業者", "記録者", "申請者", "氏名", "person", "staff", "assignee"),
    "partner": ("取引先", "顧客", "得意先", "仕入先", "客先", "会社名", "customer", "client",
                "supplier", "vendor"),
    "document_number": ("伝票番号", "注文番号", "受注番号", "発注番号", "請求番号", "管理番号",
                        "orderno", "invoiceno", "documentno"),
    "amount": ("金額", "合計金額", "売上", "請求額", "支払額", "amount", "price", "total"),
    "unit_price": ("単価", "unitprice"),
    "due_date": ("期日", "期限", "納期", "支払日", "締切", "duedate", "deadline"),
    "note": ("備考", "メモ", "コメント", "摘要", "note", "remark", "memo", "comment"),
}


def _normalized(value: str) -> str:
    return re.sub(r"[^0-9a-zぁ-んァ-ヶ一-龠]", "", unicodedata.normalize("NFKC", value).lower())


def suggest_role(name: str) -> str:
    normalized = _normalized(name)
    # 具体的な名称を先に判定する。「不良数」を単なる数量にしない。
    matches = [(len(_normalized(word)), role) for role, words in ROLE_WORDS.items()
               for word in words if _normalized(word) in normalized]
    return max(matches)[1] if matches else "none"


def infer_kind(values: list[str]) -> str:
    samples = [value.strip() for value in values if value.strip()][:30]
    if not samples:
        return "text"
    lowered = {value.casefold() for value in samples}
    if lowered <= {"true", "false", "yes", "no", "はい", "いいえ", "0", "1", "ok", "ng"}:
        return "bool"
    numeric = 0
    for value in samples:
        try:
            float(value.replace(",", ""))
            numeric += 1
        except ValueError:
            pass
    if numeric / len(samples) >= .8:
        return "number"
    datetime_count = 0
    date_only = True
    for value in samples:
        candidate = value.replace("年", "-").replace("月", "-").replace("日", "").replace("/", "-")
        try:
            parsed = datetime.fromisoformat(candidate)
            datetime_count += 1
            date_only = date_only and parsed.hour == parsed.minute == parsed.second == 0 and not re.search(r"\d:\d", value)
        except ValueError:
            pass
    if datetime_count / len(samples) >= .8:
        return "date" if date_only else "datetime"
    return "text"


def _header_index(rows: list[list[str]]) -> int:
    candidates = []
    for index, row in enumerate(rows[:20]):
        values = [value.strip() for value in row[:MAX_SAMPLE_COLUMNS] if value.strip()]
        if len(values) < 2:
            continue
        unique = len({_normalized(value) for value in values})
        text = sum(not value.replace(",", "").replace(".", "", 1).isdigit() for value in values)
        candidates.append((unique * 3 + text - index * .1, index))
    if not candidates:
        raise ValueError("見出し行を判定できませんでした。1行目付近に列名がある表を選んでください。")
    return max(candidates)[1]


def _profile(filename: str, rows: list[list[str]], sheet: str | None = None) -> SampleDataProfile:
    if not rows:
        raise ValueError("データが空です。")
    header_index = _header_index(rows)
    raw_headers = rows[header_index][:MAX_SAMPLE_COLUMNS]
    headers: list[str] = []
    used: set[str] = set()
    for index, raw in enumerate(raw_headers):
        name = raw.strip() or f"列{index + 1}"
        original = name
        suffix = 2
        while name.casefold() in used:
            name = f"{original}_{suffix}"
            suffix += 1
        used.add(name.casefold())
        headers.append(name[:100])
    data_rows = [row for row in rows[header_index + 1:] if any(value.strip() for value in row)]
    columns = []
    for index, name in enumerate(headers):
        values = [row[index].strip() for row in data_rows if index < len(row) and row[index].strip()]
        columns.append(SampleColumn(name=name, kind=infer_kind(values), samples=values[:5],
                                    suggested_role=suggest_role(name)))
    warnings = []
    if header_index:
        warnings.append(f"{header_index + 1}行目を見出しとして読み取りました。")
    if len(raw_headers) >= MAX_SAMPLE_COLUMNS:
        warnings.append("列数が多いため、先頭100列を読み取りました。")
    return SampleDataProfile(filename=filename, sheet=sheet, row_count=len(data_rows),
                             columns=columns, warnings=warnings)


def parse_csv(filename: str, data: bytes) -> SampleDataProfile:
    decoded = None
    encoding = ""
    for candidate in ("utf-8-sig", "cp932", "shift_jis"):
        try:
            decoded = data.decode(candidate)
            encoding = candidate
            break
        except UnicodeDecodeError:
            pass
    if decoded is None:
        raise ValueError("文字コードを判定できませんでした。UTF-8またはShift-JISで保存してください。")
    sample = decoded[:8192]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t;")
    except csv.Error:
        dialect = csv.excel_tab if filename.lower().endswith(".tsv") else csv.excel
    rows = list(csv.reader(io.StringIO(decoded), dialect))[:MAX_SAMPLE_ROWS + 20]
    result = _profile(filename, rows)
    if encoding != "utf-8-sig":
        result.warnings.append("Shift-JISとして読み取りました。")
    return result


def _xlsx_cell_value(cell, shared: list[str], namespace: str) -> str:
    cell_type = cell.attrib.get("t")
    if cell_type == "inlineStr":
        return "".join(node.text or "" for node in cell.findall(f".//{{{namespace}}}t"))
    value = cell.find(f"{{{namespace}}}v")
    if value is None or value.text is None:
        return ""
    if cell_type == "s":
        try:
            return shared[int(value.text)]
        except (ValueError, IndexError):
            return ""
    return value.text


def parse_xlsx(filename: str, data: bytes) -> SampleDataProfile:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise ValueError("Excelファイルを読み取れませんでした。.xlsx形式で保存してください。") from None
    with archive:
        infos = archive.infolist()
        if len(infos) > 2000 or sum(item.file_size for item in infos) > MAX_XLSX_UNCOMPRESSED_BYTES:
            raise ValueError("Excelファイルの展開後サイズが大きすぎます。必要なシートだけにして保存してください。")
        if any(item.flag_bits & 0x1 for item in infos):
            raise ValueError("パスワード付きExcelは読み取れません。保護を外した見本を用意してください。")
        names = set(archive.namelist())
        if "xl/workbook.xml" not in names:
            raise ValueError("Excelファイルを読み取れませんでした。")
        def xml(name: str) -> bytes:
            content = archive.read(name)
            if b"<!DOCTYPE" in content.upper() or b"<!ENTITY" in content.upper():
                raise ValueError("Excelファイル内のXMLを安全に読み取れませんでした。")
            return content
        shared: list[str] = []
        if "xl/sharedStrings.xml" in names:
            root = ET.fromstring(xml("xl/sharedStrings.xml"))
            shared = ["".join(node.text or "" for node in item.iter() if node.tag.endswith("}t"))
                      for item in root if item.tag.endswith("}si")]
        workbook = ET.fromstring(xml("xl/workbook.xml"))
        relationship_id = None
        sheet_name = None
        for node in workbook.iter():
            if node.tag.endswith("}sheet"):
                sheet_name = node.attrib.get("name", "Sheet1")
                relationship_id = next((value for key, value in node.attrib.items() if key.endswith("}id")), None)
                break
        target = "worksheets/sheet1.xml"
        rel_path = "xl/_rels/workbook.xml.rels"
        if relationship_id and rel_path in names:
            rels = ET.fromstring(xml(rel_path))
            for node in rels:
                if node.attrib.get("Id") == relationship_id:
                    target = node.attrib.get("Target", target).lstrip("/")
                    break
        sheet_path = target if target.startswith("xl/") else str(PurePosixPath("xl") / target)
        if sheet_path not in names:
            raise ValueError("Excelの先頭シートを読み取れませんでした。")
        root = ET.fromstring(xml(sheet_path))
        namespace = root.tag.split("}", 1)[0].lstrip("{")
        rows: list[list[str]] = []
        for row in root.findall(f".//{{{namespace}}}row")[:MAX_SAMPLE_ROWS + 20]:
            values: list[str] = []
            for cell in row.findall(f"{{{namespace}}}c"):
                reference = cell.attrib.get("r", "A1")
                letters = re.match(r"[A-Z]+", reference)
                column = 0
                for letter in letters.group(0) if letters else "A":
                    column = column * 26 + ord(letter) - 64
                if column > MAX_SAMPLE_COLUMNS:
                    continue
                while len(values) < column:
                    values.append("")
                values[column - 1] = _xlsx_cell_value(cell, shared, namespace)
            rows.append(values)
        return _profile(filename, rows, sheet_name)


def analyze_sample(filename: str, data: bytes) -> SampleDataProfile:
    if not data or len(data) > MAX_SAMPLE_BYTES:
        raise ValueError("5MiB以下のCSVまたはExcelファイルを選んでください。")
    lower = filename.lower()
    if lower.endswith((".csv", ".tsv")):
        return parse_csv(filename, data)
    if lower.endswith(".xlsx"):
        try:
            return parse_xlsx(filename, data)
        except ValueError:
            raise
        except (ET.ParseError, KeyError, IndexError, zipfile.BadZipFile):
            raise ValueError("Excelファイルを読み取れませんでした。.xlsx形式で保存し直してください。") from None
    raise ValueError("CSV、TSV、または.xlsx形式のExcelファイルを選んでください。")
