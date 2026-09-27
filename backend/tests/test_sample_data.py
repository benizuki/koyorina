import io
import zipfile

from backend.domain.sample_data import analyze_sample, suggest_role


def test_cp932_manufacturing_csv_infers_columns_and_roles():
    data = ("設備名,記録日時,生産数,不良内容,測定値\n"
            "1号機,2026-09-24 06:10,12,キズ,10.2\n"
            "1号機,2026-09-24 07:10,15,,10.3\n").encode("cp932")

    result = analyze_sample("設備実績.csv", data)

    assert result.row_count == 2
    assert [column.suggested_role for column in result.columns] == [
        "equipment", "timestamp", "quantity", "defect_category", "quality_value"]
    assert result.columns[2].kind == "number"
    assert "Shift-JIS" in result.warnings[-1]


def test_xlsx_shared_strings_are_read_without_external_library():
    output = io.BytesIO()
    shared = ["開始時刻", "終了時刻", "稼働状態", "2026-09-24 06:00", "2026-09-24 07:00", "稼働"]
    strings = "".join(f"<si><t>{value}</t></si>" for value in shared)
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("xl/workbook.xml", '''<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="稼働実績" sheetId="1" r:id="rId1"/></sheets></workbook>''')
        archive.writestr("xl/_rels/workbook.xml.rels", '''<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>''')
        archive.writestr("xl/sharedStrings.xml", f'''<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">{strings}</sst>''')
        archive.writestr("xl/worksheets/sheet1.xml", '''<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>
          <row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c><c r="C1" t="s"><v>2</v></c></row>
          <row r="2"><c r="A2" t="s"><v>3</v></c><c r="B2" t="s"><v>4</v></c><c r="C2" t="s"><v>5</v></c></row>
        </sheetData></worksheet>''')

    result = analyze_sample("稼働.xlsx", output.getvalue())

    assert result.sheet == "稼働実績"
    assert [column.suggested_role for column in result.columns] == ["start_time", "end_time", "status"]


def test_unknown_and_oversized_formats_are_rejected():
    for name, data in (("sample.xls", b"old excel"), ("large.csv", b"x" * (5 * 1024 * 1024 + 1))):
        try:
            analyze_sample(name, data)
        except ValueError:
            pass
        else:
            raise AssertionError("unsupported sample must be rejected")


def test_plan_and_actual_time_columns_use_generic_start_and_end_roles():
    assert suggest_role("計画開始日時") == "start_time"
    assert suggest_role("実績開始") == "start_time"
    assert suggest_role("予定終了") == "end_time"
    assert suggest_role("実績終了日時") == "end_time"


def test_highly_compressed_xlsx_is_rejected_before_xml_expansion():
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("xl/workbook.xml", b"0" * (26 * 1024 * 1024))
    try:
        analyze_sample("large.xlsx", output.getvalue())
    except ValueError as exc:
        assert "展開後サイズ" in str(exc)
    else:
        raise AssertionError("zip bomb must be rejected")


def test_back_office_columns_are_recognised():
    assert [suggest_role(name) for name in (
        "取引先名", "得意先", "金額", "合計金額", "単価", "担当者", "保管場所", "部署",
        "伝票番号", "納期", "備考", "売上日",
    )] == ["partner", "partner", "amount", "amount", "unit_price", "person", "location",
           "department", "document_number", "due_date", "note", "timestamp"]
