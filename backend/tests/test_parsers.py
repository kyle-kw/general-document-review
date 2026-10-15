import base64
import io
import json
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
import respx
from docx import Document
from docx.oxml import parse_xml
from openpyxl import Workbook
from openpyxl.chart import BarChart, Reference
from pptx import Presentation
from pptx.chart.data import CategoryChartData
from pptx.enum.chart import XL_CHART_TYPE
from pptx.util import Inches
from pypdf import PdfReader, PdfWriter
from reportlab.pdfgen.canvas import Canvas

from app.config import Settings
from app.parsers import FileParsingError, ParserCancelled, parse_document
from app.storage import validate_file


def settings(tmp_path: Path, **changes: object) -> Settings:
    return Settings(_env_file=None, data_dir=tmp_path, **changes)


async def test_docx_recursive_tables_headers_stable_source_locations(tmp_path: Path) -> None:
    source = Document()
    paragraph = source.add_paragraph()
    paragraph.add_run("计划投入 ").bold = True
    paragraph.add_run("100 万元。")
    table = source.add_table(rows=1, cols=1)
    table.cell(0, 0).text = "表格目标 90%"
    nested = table.cell(0, 0).add_table(rows=1, cols=1)
    nested.cell(0, 0).text = "嵌套表格 80%"
    source.sections[0].header.paragraphs[0].text = "年度项目报告"
    path = tmp_path / "source.docx"
    source.save(path)
    first = await parse_document(path, "main", path.name, settings(tmp_path), tmp_path)
    second = await parse_document(path, "main", path.name, settings(tmp_path), tmp_path)
    assert [block.id for block in first.blocks] == [block.id for block in second.blocks]
    assert {block.text for block in first.blocks} == {"计划投入 100 万元。", "表格目标 90%", "嵌套表格 80%", "年度项目报告"}
    main = next(block for block in first.blocks if block.text.startswith("计划"))
    assert main.location["runs"][1]["start"] == len("计划投入 ")
    assert main.location["commentable"] is True
    header = next(block for block in first.blocks if block.text == "年度项目报告")
    assert header.location["commentable"] is False
    assert any(block.kind == "table_cell" and "嵌套" in block.text for block in first.blocks)


async def test_docx_inline_content_controls_are_extracted_with_source_offsets(tmp_path: Path) -> None:
    source = Document()
    paragraph = source.add_paragraph("Budget: ")
    paragraph._p.append(parse_xml('''
        <w:sdt xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
          <w:sdtPr><w:tag w:val="amount"/></w:sdtPr>
          <w:sdtContent>
            <w:r><w:t>100</w:t><w:tab/></w:r>
            <w:sdt><w:sdtPr/><w:sdtContent><w:r><w:t>million</w:t></w:r></w:sdtContent></w:sdt>
          </w:sdtContent>
        </w:sdt>'''))
    paragraph.add_run(" USD.")
    control_only = source.add_paragraph()
    control_only._p.append(parse_xml('''
        <w:sdt xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
          <w:sdtPr/><w:sdtContent><w:r><w:t>Required clause</w:t></w:r></w:sdtContent>
        </w:sdt>'''))
    source.add_paragraph("Ordinary paragraph")
    path = tmp_path / "controls.docx"
    source.save(path)

    parsed = await parse_document(path, "main", path.name, settings(tmp_path), tmp_path)

    assert [block.text for block in parsed.blocks] == ["Budget: 100\tmillion USD.", "Required clause", "Ordinary paragraph"]
    controlled = parsed.blocks[0]
    assert controlled.location["char_end"] == len(controlled.text)
    assert [(run["start"], run["end"]) for run in controlled.location["runs"]] == [(0, 8), (8, 12), (12, 19), (19, 24)]
    assert all(not block.location["commentable"] and not block.location["simple_runs"] for block in parsed.blocks[:2])
    assert parsed.blocks[2].location["commentable"] is True
    assert not parsed.warnings


async def test_xlsx_formula_cache_missing_is_not_zero_and_chart_values(tmp_path: Path) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "预算"
    sheet.append(["项目", "收入"])
    sheet.append(["甲", 100])
    sheet.append(["乙", "=B2*2"])
    sheet.merge_cells("D1:E1")
    sheet["D1"] = "合并标题"
    chart = BarChart()
    chart.add_data(Reference(sheet, min_col=2, min_row=1, max_row=2), titles_from_data=True)
    chart.set_categories(Reference(sheet, min_col=1, min_row=2, max_row=2))
    sheet.add_chart(chart, "G1")
    path = tmp_path / "book.xlsx"
    workbook.save(path)
    parsed = await parse_document(path, "book", path.name, settings(tmp_path), tmp_path)
    assert any(block.text == "100" and block.location["cell"] == "B2" for block in parsed.blocks)
    assert not any(block.location.get("cell") == "B3" for block in parsed.blocks)
    assert any(w.code == "formula_cache_missing" and w.location["cell"] == "B3" for w in parsed.warnings)
    assert any(w.code == "chart_cache_missing" for w in parsed.warnings)
    merged = next(block for block in parsed.blocks if block.text == "合并标题")
    assert merged.location["merged_range"] == "D1:E1"


@pytest.mark.parametrize(("references", "nonstandard_part"), [
    (["A1:XFD1048576"], False),
    (["A1:A600000", "B1:B600000"], True),
])
async def test_xlsx_rejects_merge_expansion_before_loading_cells(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, references: list[str], nonstandard_part: bool,
) -> None:
    path = tmp_path / "merge-bomb.xlsx"
    workbook = Workbook()
    workbook.active["A1"] = "Budget 100"
    workbook.save(path)
    with zipfile.ZipFile(path) as archive:
        parts = {name: archive.read(name) for name in archive.namelist()}
    worksheet = "xl/worksheets/sheet1.xml"
    merges = '<mergeCells count="{}">{}</mergeCells>'.format(
        len(references), "".join(f'<mergeCell ref="{reference}"/>' for reference in references))
    parts[worksheet] = parts[worksheet].replace(b"</worksheet>", f"{merges}</worksheet>".encode())
    if nonstandard_part:
        parts["xl/worksheets/custom.data"] = parts.pop(worksheet)
        for name in ("xl/_rels/workbook.xml.rels", "[Content_Types].xml"):
            parts[name] = parts[name].replace(b"worksheets/sheet1.xml", b"worksheets/custom.data")
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in parts.items():
            archive.writestr(name, data)
    # Even a very small accepted upload must not expand billions of cells.
    validate_file(path, max_unpacked_mb=1)
    monkeypatch.setattr("app.parsers.load_workbook", lambda *args, **kwargs: pytest.fail("unsafe normal workbook loading"))

    with pytest.raises(FileParsingError, match="1,000,000"):
        await parse_document(path, "book", path.name, settings(tmp_path), tmp_path)


async def test_pptx_groups_tables_notes_and_native_chart_cache(tmp_path: Path) -> None:
    presentation = Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[6])
    group = slide.shapes.add_group_shape()
    group.shapes.add_textbox(Inches(1), Inches(1), Inches(3), Inches(1)).text = "分组里的内容"
    table = slide.shapes.add_table(1, 2, Inches(1), Inches(3), Inches(4), Inches(1)).table
    table.cell(0, 0).text = "总额"
    table.cell(0, 1).text = "100"
    data = CategoryChartData()
    data.categories = ["一月", "二月"]
    data.add_series("收入", [100, 150])
    slide.shapes.add_chart(XL_CHART_TYPE.COLUMN_CLUSTERED, Inches(5), Inches(1), Inches(4), Inches(3), data)
    slide.notes_slide.notes_text_frame.text = "演讲备注 2026"
    path = tmp_path / "slides.pptx"
    presentation.save(path)
    parsed = await parse_document(path, "slides", path.name, settings(tmp_path), tmp_path)
    assert any(block.text == "分组里的内容" and len(block.location["shape_path"]) == 2 for block in parsed.blocks)
    assert any(block.kind == "notes" and "2026" in block.text for block in parsed.blocks)
    assert any(block.kind == "table_cell" and block.text == "100" for block in parsed.blocks)
    assert any(block.kind == "chart" and "150" in block.text and "二月" in block.text for block in parsed.blocks)
    chart = next(block for block in parsed.blocks if block.kind == "chart")
    assert chart.location["slide"] == 1
    assert not any(w.code == "embedded_objects_excluded" for w in parsed.warnings)


async def test_smartart_is_explicitly_reported_as_not_covered(tmp_path: Path) -> None:
    source = Document()
    source.add_paragraph("正文可读取")
    path = tmp_path / "diagram.docx"
    source.save(path)
    with zipfile.ZipFile(path, "a") as archive:
        archive.writestr("word/diagrams/data1.xml", "<diagram/>")
    parsed = await parse_document(path, "doc", path.name, settings(tmp_path), tmp_path)
    assert any(w.code == "smartart_excluded" and w.incomplete for w in parsed.warnings)


async def test_pdf_text_coordinates_and_scan_coverage_warning(tmp_path: Path) -> None:
    path = tmp_path / "local.pdf"
    canvas = Canvas(str(path))
    canvas.drawString(72, 700, "Budget 100")
    canvas.showPage()
    canvas.showPage()
    canvas.save()
    parsed = await parse_document(path, "pdf", path.name, settings(tmp_path), tmp_path)
    assert any(block.text == "Budget 100" and block.location["page"] == 1 and block.location["bbox"] for block in parsed.blocks)
    assert any(w.code == "pdf_no_text" and w.location["page"] == 2 for w in parsed.warnings)


async def test_pdf_body_text_beside_table_survives_character_filtering(tmp_path: Path) -> None:
    path = tmp_path / "table-and-body.pdf"
    canvas = Canvas(str(path))
    for y in (700, 680, 660):
        canvas.line(100, y, 450, y)
    for x in (100, 275, 450):
        canvas.line(x, 660, x, 700)
    for text, x, y in (("Project", 110, 685), ("Budget", 285, 685), ("Alpha", 110, 665),
                       ("100", 285, 665), ("Total 900", 470, 665)):
        canvas.drawString(x, y, text)
    canvas.save()

    parsed = await parse_document(path, "pdf", path.name, settings(tmp_path), tmp_path)

    assert [block.text for block in parsed.blocks if block.kind == "table_row"] == ["Project | Budget", "Alpha | 100"]
    body = [block for block in parsed.blocks if block.kind == "paragraph"]
    assert [block.text for block in body] == ["Total 900"]
    assert body[0].location["bbox"][0] == 470
    assert not parsed.warnings


async def test_ocr_batches_auth_count_validation_and_saved_response(tmp_path: Path) -> None:
    path = tmp_path / "scan.pdf"
    writer = PdfWriter()
    for _ in range(12):
        writer.add_blank_page(width=100, height=100)
    writer.write(path)
    counts: list[int] = []

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        counts.append(len(PdfReader(io.BytesIO(base64.b64decode(payload["file"]))).pages))
        assert payload["fileType"] == 0
        assert payload["restructurePages"] is False
        assert request.headers["Authorization"] == "token secret"
        count = counts[-1] if len(counts) == 1 else 1  # Deliberately omit the last page.
        return httpx.Response(200, json={"errorCode": 0, "result": {"layoutParsingResults": [
            {"markdown": {"text": f"批次 {len(counts)} 第 {i + 1} 页"}, "prunedResult": {}}
            for i in range(count)
        ]}})

    with respx.mock as router:
        router.post("https://ocr.test/layout-parsing").mock(side_effect=respond)
        parsed = await parse_document(path, "ocr", path.name,
                                      settings(tmp_path, paddleocr_url="https://ocr.test", paddleocr_token="secret"), tmp_path)
    assert counts == [10, 2]
    assert [block.location["page"] for block in parsed.blocks] == list(range(1, 12))
    assert any(w.code == "ocr_page_count_mismatch" for w in parsed.warnings)
    assert any(w.code == "ocr_missing_page" and w.location["page"] == 12 for w in parsed.warnings)
    assert len(list(tmp_path.glob("ocr-ocr-*.json"))) == 2


async def test_ocr_failure_does_not_silently_use_local_text(tmp_path: Path) -> None:
    path = tmp_path / "failure.pdf"
    canvas = Canvas(str(path))
    canvas.drawString(72, 700, "Locally extractable")
    canvas.save()
    with respx.mock as router:
        router.post("https://ocr.test/layout-parsing").respond(503, json={"errorMsg": "busy"})
        parsed = await parse_document(path, "pdf", path.name, settings(tmp_path, paddleocr_url="https://ocr.test", llm_max_retries=0), tmp_path)
    assert not parsed.blocks
    assert any(w.code == "ocr_batch_failed" for w in parsed.warnings)


@pytest.mark.parametrize("bad_page", [None, {"markdown": None}, {"markdown": {"text": "bad"}, "prunedResult": []}])
async def test_ocr_malformed_pages_and_batches_preserve_other_pages(tmp_path: Path, bad_page: object) -> None:
    path = tmp_path / "partial-ocr.pdf"
    writer = PdfWriter()
    for _ in range(6):
        writer.add_blank_page(width=100, height=100)
    writer.write(path)
    with respx.mock as router:
        route = router.post("https://ocr.test/layout-parsing").mock(side_effect=[
            httpx.Response(200, json={"result": {"layoutParsingResults": [{"markdown": {"text": "page one"}}, bad_page]}}),
            httpx.Response(200, json=[]),
            httpx.Response(200, json={"result": {"layoutParsingResults": [
                {"markdown": {"text": "page five"}, "prunedResult": None}, {"markdown": {"text": "page six"}}]}}),
        ])
        parsed = await parse_document(path, "pdf", path.name,
                                      settings(tmp_path, paddleocr_url="https://ocr.test", paddleocr_batch_pages=2), tmp_path)

    assert route.call_count == 3
    assert [(block.location["page"], block.text) for block in parsed.blocks] == [(1, "page one"), (5, "page five"), (6, "page six")]
    assert any(w.code == "ocr_page_failed" and w.location["page"] == 2 and w.incomplete for w in parsed.warnings)
    assert any(w.code == "ocr_batch_failed" and w.location["page_start"] == 3 and w.location["page_end"] == 4 for w in parsed.warnings)


async def test_password_protected_pdf_and_invalid_office_are_errors(tmp_path: Path) -> None:
    path = tmp_path / "encrypted.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.encrypt("password")
    writer.write(path)
    with pytest.raises(FileParsingError, match="密码"):
        await parse_document(path, "pdf", path.name, settings(tmp_path), tmp_path)
    broken = tmp_path / "bad.docx"
    broken.write_bytes(b"invalid")
    with pytest.raises(FileParsingError):
        await parse_document(broken, "doc", broken.name, settings(tmp_path), tmp_path)


async def test_ocr_retries_transient_errors_but_not_auth(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "scan.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.write(path)
    delay = AsyncMock()
    monkeypatch.setattr("app.parsers._retry_delay", delay)
    good = {"result": {"layoutParsingResults": [{"markdown": {"text": "完成"}}]}}
    with respx.mock as router:
        route = router.post("https://ocr.test/layout-parsing").mock(side_effect=[
            httpx.Response(503, json={"errorMsg": "busy"}), httpx.Response(200, json=good)])
        parsed = await parse_document(path, "pdf", path.name, settings(tmp_path, paddleocr_url="https://ocr.test"), tmp_path)
        assert route.call_count == 2
        assert parsed.blocks[0].text == "完成"
    assert delay.await_count == 1
    with respx.mock as router:
        route = router.post("https://ocr.test/layout-parsing").respond(401, json={"errorMsg": "unauthorized"})
        parsed = await parse_document(path, "pdf", path.name, settings(tmp_path, paddleocr_url="https://ocr.test"), tmp_path)
        assert route.call_count == 1
        assert any(w.code == "ocr_batch_failed" for w in parsed.warnings)


async def test_parser_cooperatively_cancels_during_office_and_ocr(tmp_path: Path) -> None:
    document = Document()
    for index in range(10):
        document.add_paragraph(str(index))
    path = tmp_path / "cancel.docx"
    document.save(path)
    checks = 0

    def cancelled() -> bool:
        nonlocal checks
        checks += 1
        return checks > 5

    with pytest.raises(ParserCancelled):
        await parse_document(path, "doc", path.name, settings(tmp_path), tmp_path, cancelled=cancelled)
    pdf_path = tmp_path / "cancel.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.write(pdf_path)
    done = False

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal done
        done = True
        return httpx.Response(200, json={"result": {"layoutParsingResults": []}})

    with respx.mock as router:
        router.post("https://ocr.test/layout-parsing").mock(side_effect=respond)
        with pytest.raises(ParserCancelled):
            await parse_document(pdf_path, "pdf", pdf_path.name, settings(tmp_path, paddleocr_url="https://ocr.test"), tmp_path,
                                 cancelled=lambda: done)
