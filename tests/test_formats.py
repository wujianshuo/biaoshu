import os
import unittest
from io import BytesIO
from unittest.mock import patch
from zipfile import ZipFile

from docx import Document
from openpyxl import Workbook, load_workbook
from PIL import Image

from biaoshu.formats import FormatError, parse_document, render_docx, render_matrix, render_pdf


class FormatTests(unittest.TestCase):
    def test_pdf_origin_and_bordered_table(self):
        """Create an in-memory, text-only PDF without external fixtures."""
        page_one = b"BT /F1 12 Tf 50 770 Td (Contract duration: 112 days) Tj ET\n"
        page_two = b"""\
BT /F1 12 Tf 50 700 Td (Code) Tj ET
BT /F1 12 Tf 170 700 Td (Quantity) Tj ET
BT /F1 12 Tf 50 670 Td (0007) Tj ET
BT /F1 12 Tf 170 670 Td (6) Tj ET
40 720 m 260 720 l 40 690 m 260 690 l 40 660 m 260 660 l S
40 660 m 40 720 l 155 660 m 155 720 l 260 660 m 260 720 l S
"""
        def stream(contents):
            return f"<< /Length {len(contents)} >>\nstream\n".encode() + contents + b"\nendstream"

        objects = [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R 4 0 R] /Count 2 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 5 0 R >> >> /Contents 6 0 R >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 5 0 R >> >> /Contents 7 0 R >>",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
            stream(page_one), stream(page_two),
        ]
        data = bytearray(b"%PDF-1.4\n")
        offsets = []
        for index, value in enumerate(objects, 1):
            offsets.append(len(data))
            data.extend(f"{index} 0 obj\n".encode() + value + b"\nendobj\n")
        xref_offset = len(data)
        data.extend(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
        for offset in offsets:
            data.extend(f"{offset:010d} 00000 n \n".encode())
        data.extend(f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n".encode())

        result = parse_document("招标.pdf", bytes(data))
        self.assertEqual(result["status"], "parsed")
        self.assertEqual(result["snippets"][0]["locator"], "第 1 页")
        table = [item for item in result["snippets"] if item["kind"] == "table_row"]
        self.assertEqual(table[1]["page"], 2)
        self.assertEqual(table[1]["cells"], ["0007", "6"])
        self.assertEqual(table[1]["locator"], "第 2 页 表 1 行 2")

    def test_pdf_scan_preserves_real_page_and_needs_review(self):
        source = BytesIO()
        Image.new("RGB", (300, 200), color="white").save(source, format="PDF")
        with patch.dict(os.environ, {"BIAOSHU_OCR_BIN": ""}):
            result = parse_document("扫描件.pdf", source.getvalue())
        self.assertEqual(result["status"], "needs_review")
        self.assertEqual(result["snippets"], [])
        self.assertIn("第 1 页", result["warnings"][0])
        with (patch.dict(os.environ, {"BIAOSHU_OCR_BIN": "test-fixture"}),
              patch("biaoshu.formats._ocr_image", return_value="工期112日历天")):
            result = parse_document("扫描件.pdf", source.getvalue())
        self.assertEqual(result["status"], "needs_review")
        self.assertEqual(result["snippets"][0]["kind"], "ocr")
        self.assertEqual(result["snippets"][0]["page"], 1)

    def test_docx_outline_and_tables(self):
        document = Document()
        document.add_heading("评标细则", level=1)
        document.add_paragraph("详见评分表。")
        row = document.add_table(rows=1, cols=2).rows[0]
        row.cells[0].text = "施工工艺"
        row.cells[1].text = "5分"
        source = BytesIO()
        document.save(source)
        snippets = parse_document("招标.docx", source.getvalue())["snippets"]
        self.assertEqual([s["kind"] for s in snippets], ["heading", "paragraph", "table_row"])
        self.assertEqual(snippets[2]["section"], "评标细则")
        self.assertEqual(snippets[2]["locator"], "表 1 行 1")
        self.assertIsNone(snippets[2]["page"])

    def test_xlsx_coordinates_formula_and_leading_zeros(self):
        workbook = Workbook()
        original = workbook.active
        original.title = "分部分项"
        original.append(["000042", 17, "=1+2"])
        original["B1"].number_format = "0000"
        workbook.create_sheet("措施表").append(["措施项目"])
        source = BytesIO()
        workbook.save(source)
        result = parse_document("清单.xlsx", source.getvalue())
        self.assertEqual(result["status"], "needs_review")
        self.assertEqual([s["locator"] for s in result["snippets"]], ["分部分项!A1:C1", "措施表!A1"])
        row = result["snippets"][0]["cells"]
        self.assertEqual(row[0]["value"], "000042")
        self.assertEqual(row[1]["display"], "0017")
        self.assertEqual(row[2], {"cell": "C1", "formula": "=1+2", "value": None})

    def test_txt_validation_and_cad_needs_manual_export(self):
        result = parse_document("附件.txt", "第一条\n第二条".encode("gb18030"))
        self.assertEqual(result["snippets"][1]["text"], "第二条")
        self.assertEqual(parse_document("图纸.DWG", b"AC1032dummy")["status"], "unsupported")
        with self.assertRaises(FormatError):
            parse_document("../附件.txt", b"entry")
        with self.assertRaises(FormatError):
            parse_document("资质.docx", b"wrong-format")

    def test_xlsx_export_treats_untrusted_text_as_literal(self):
        data = render_matrix([{"id": "0001", "offer": "=1+2", "score": 5}], "评分矩阵",
                             [("id", "编号"), ("offer", "原文"), ("score", "分值")])
        workbook = load_workbook(BytesIO(data), data_only=False)
        sheet = workbook.active
        self.assertEqual(sheet["A2"].value, "0001")
        self.assertEqual(sheet["B2"].value, "=1+2")
        self.assertEqual(sheet["B2"].data_type, "s")
        self.assertEqual(sheet["C2"].data_type, "n")
        self.assertEqual(sheet.freeze_panes, "A2")
        workbook.close()

    def test_docx_headings_table_toc_and_page_field(self):
        data = render_docx("技术标初稿", [
            {"chapter_name": "工程概况", "content": "工程工期详见招标文件。"},
            {"chapter_name": "施工安排", "content": "## 节点\n| 工序 | 时间 |\n| --- | --- |\n| 测量 | 第 1 天 |"},
        ])
        restored = Document(BytesIO(data))
        self.assertEqual(restored.tables[0].cell(1, 1).text, "第 1 天")
        self.assertEqual([p.text for p in restored.paragraphs if p.style.name == "Heading 1"],
                         ["工程概况", "施工安排"])
        with ZipFile(BytesIO(data)) as archive:
            self.assertIn(b"TOC", archive.read("word/document.xml"))
            self.assertIn(b"PAGE", archive.read("word/footer1.xml"))

    def test_docx_template_preserves_marker_position_and_other_content(self):
        template = Document()
        template.add_paragraph("招标人固定段落一")
        template.add_paragraph("{{BID_BODY}}")
        template.add_paragraph("招标人固定段落二")
        source = BytesIO()
        template.save(source)
        data = render_docx("技术标", [{"title": "施工计划", "content": "工期待确认。"}], template=source.getvalue())
        self.assertEqual([p.text for p in Document(BytesIO(data)).paragraphs],
                         ["招标人固定段落一", "技术标", "施工计划", "工期待确认。", "招标人固定段落二"])

    def test_pdf_export_configuration_is_explicit(self):
        with patch.dict(os.environ, {"BIAOSHU_SOFFICE_BIN": ""}):
            with self.assertRaisesRegex(FormatError, "未配置本地 LibreOffice"):
                render_pdf(b"fixture")


if __name__ == "__main__":
    unittest.main()
