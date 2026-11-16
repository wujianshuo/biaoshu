"""Read tender materials and render reviewable Office drafts locally."""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
from collections.abc import Mapping, Sequence
from datetime import date, datetime, time, timedelta
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile

import pdfplumber
from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor
from docx.table import Table
from docx.text.paragraph import Paragraph
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from pdfminer.pdfdocument import PDFPasswordIncorrect, PDFTextExtractionNotAllowed
from pdfminer.pdfparser import PDFSyntaxError


MAX_UPLOAD_BYTES = 32 * 1024 * 1024
MAX_OOXML_BYTES = 128 * 1024 * 1024
MAX_OOXML_MEMBERS = 4096
MAX_PDF_PAGES = 1000
MAX_SHEET_CELLS = 1_000_000
MAX_SNIPPET_LENGTH = 3500
MAX_OCR_PIXELS = 16_000_000
MAX_OCR_PAGES = 40
_CELL_TEXT_LIMIT = 32767
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_IMAGE_TYPES = {"png", "jpg", "jpeg", "tif", "tiff"}


class FormatError(ValueError):
    """A local parser or document generator could not handle the supplied input."""


def _verify_ooxml(data: bytes, required_entry: str) -> None:
    try:
        with ZipFile(BytesIO(data)) as archive:
            files = archive.infolist()
            if len(files) > MAX_OOXML_MEMBERS or sum(item.file_size for item in files) > MAX_OOXML_BYTES:
                raise FormatError("Office 文件解压后的内容超过解析上限。请拆分文件。")
            names = {item.filename for item in files}
            if len(names) != len(files) or any(item.flag_bits & 1 for item in files):
                raise FormatError("Office 文档包含重复或加密条目。请重新保存后上传。")
            if required_entry not in names:
                raise FormatError("文件内容与扩展名不匹配。")
    except BadZipFile as exc:
        raise FormatError("Office 文件已损坏或加密。请提供可打开的原件。") from exc


def _result(kind: str, snippets: list[dict], warnings: list[str], *, status: str | None = None) -> dict:
    if not snippets and not warnings:
        warnings.append("未提取到可用正文。请核对原件。")
    return {
        "format": kind,
        "status": status or ("needs_review" if warnings else "parsed"),
        "snippets": snippets,
        "warnings": warnings,
    }


def _text_chunks(text: str):
    text = _CONTROL_CHARACTERS.sub("", text).strip()
    while len(text) > MAX_SNIPPET_LENGTH:
        offset = text.rfind("\n", MAX_SNIPPET_LENGTH // 2, MAX_SNIPPET_LENGTH)
        offset = offset if offset > 0 else MAX_SNIPPET_LENGTH
        yield text[:offset].strip()
        text = text[offset:].strip()
    if text:
        yield text


def _push_text(snippets, text, *, kind, locator, page=None, section=None, **extra):
    chunks = list(_text_chunks(text))
    for index, chunk in enumerate(chunks, 1):
        snippets.append({
            "text": chunk,
            "page": page,
            "section": section,
            "locator": locator if len(chunks) == 1 else f"{locator} 第{index}段",
            "kind": kind,
            **extra,
        })


def _ocr_image(image) -> str:
    binary = os.environ.get("BIAOSHU_OCR_BIN")
    if not binary:
        raise FormatError("未配置本地 OCR 工具。")
    if image.width * image.height > MAX_OCR_PIXELS:
        raise FormatError("扫描图像超过 OCR 像素上限。")
    stream = BytesIO()
    image.convert("L").save(stream, format="PNG")
    try:
        result = subprocess.run(
            [binary, "stdin", "stdout", "-l", os.environ.get("BIAOSHU_OCR_LANG", "chi_sim+eng"), "--psm", "3"],
            input=stream.getvalue(), capture_output=True, check=True, timeout=60,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise FormatError("本地 OCR 执行失败。请检查可执行文件和语言数据。") from exc
    return result.stdout.decode("utf-8", errors="replace").strip()


def _parse_image(kind: str, data: bytes) -> dict:
    from PIL import Image, UnidentifiedImageError

    snippets = []
    warnings = []
    try:
        with Image.open(BytesIO(data)) as image:
            expected = {"png": "PNG", "jpg": "JPEG", "jpeg": "JPEG", "tif": "TIFF", "tiff": "TIFF"}
            if image.format != expected[kind]:
                raise FormatError("图像内容与扩展名不匹配。")
            if not os.environ.get("BIAOSHU_OCR_BIN"):
                return _result(kind, [], ["图像需要本地 OCR。请配置工具或提供已转录文本。"])
            frame_count = getattr(image, "n_frames", 1)
            if frame_count > MAX_OCR_PAGES:
                raise FormatError("图像页数超过单文件 OCR 上限。请拆分文件。")
            for frame in range(frame_count):
                image.seek(frame)
                text = _ocr_image(image)
                locator = f"第 {frame + 1} 页"
                if text:
                    _push_text(snippets, text, kind="ocr", locator=locator, page=frame + 1)
                    warnings.append(f"{locator} 来自 OCR，请对照原件复核。")
                else:
                    warnings.append(f"{locator} 的 OCR 没有返回文本。")
    except (Image.DecompressionBombError, UnidentifiedImageError, OSError) as exc:
        raise FormatError("图像已损坏、格式不匹配或超过像素限制。") from exc
    return _result(kind, snippets, warnings)


def _parse_pdf(data: bytes) -> dict:
    if b"%PDF-" not in data[:1024]:
        raise FormatError("文件内容与 PDF 扩展名不匹配。")
    snippets = []
    warnings = []
    ocr_count = 0
    ocr_pdf = None
    try:
        with pdfplumber.open(BytesIO(data)) as pdf:
            if len(pdf.pages) > MAX_PDF_PAGES:
                raise FormatError("PDF 超过单文件页数上限。请拆分文件。")
            for page in pdf.pages:
                number = page.page_number
                locator = f"第 {number} 页"
                tables = []
                try:
                    for table in page.find_tables():
                        rows = table.extract()
                        if (len(rows) >= 2 and len(rows[0]) >= 2
                                and any(value and value.strip() for row in rows for value in row)):
                            tables.append((table.bbox, rows))
                except (TypeError, ValueError, IndexError) as exc:
                    warnings.append(f"{locator} 的表格提取失败（{type(exc).__name__}）。")

                def outside_tables(item):
                    if item.get("object_type") != "char":
                        return True
                    center_x = (item["x0"] + item["x1"]) / 2
                    center_y = (item["top"] + item["bottom"]) / 2
                    return all(not (left <= center_x <= right and top <= center_y <= bottom)
                               for (left, top, right, bottom), _ in tables)

                body = page.filter(outside_tables).extract_text() if tables else page.extract_text()
                _push_text(snippets, body or "", kind="paragraph", locator=locator, page=number)
                for table_index, (bbox, rows) in enumerate(tables, 1):
                    for row, values in enumerate(rows, 1):
                        if any(value and value.strip() for value in values):
                            _push_text(snippets, " | ".join(value or "" for value in values), kind="table_row",
                                       locator=f"{locator} 表 {table_index} 行 {row}", page=number, cells=values,
                                       bbox=[round(point, 2) for point in bbox])

                if not (body and body.strip()) and not tables:
                    if not os.environ.get("BIAOSHU_OCR_BIN") or ocr_count >= MAX_OCR_PAGES:
                        warnings.append(f"{locator} 无可用文字层，可能是扫描、图纸或空白页，请复核。")
                    else:
                        ocr_count += 1
                        try:
                            if ocr_pdf is None:
                                import pypdfium2

                                ocr_pdf = pypdfium2.PdfDocument(data)
                            source_page = ocr_pdf[number - 1]
                            try:
                                width, height = source_page.get_size()
                                scale = min(3, math.sqrt(MAX_OCR_PIXELS / max(1, width * height)))
                                image = source_page.render(scale=scale)
                                try:
                                    recognized = _ocr_image(image.to_pil())
                                finally:
                                    image.close()
                            finally:
                                source_page.close()
                            _push_text(snippets, recognized, kind="ocr", locator=locator, page=number)
                            warnings.append(f"{locator} 来自 OCR，请对照原件复核。" if recognized else
                                            f"{locator} 的 OCR 没有返回文本。")
                        except (FormatError, OSError, ValueError, RuntimeError):
                            warnings.append(f"{locator} 的本地 OCR 执行失败，请复核或提供带文字层的 PDF。")
                elif any(image["width"] >= page.width * 0.25 and image["height"] >= page.height * 0.20
                         for image in page.images):
                    warnings.append(f"{locator} 含大幅图像，提取结果未包括图像文字。请核对原件。")
                page.close()
    except (PDFSyntaxError, PDFPasswordIncorrect, PDFTextExtractionNotAllowed, OSError) as exc:
        raise FormatError("PDF 已损坏、加密或禁止文本提取。") from exc
    finally:
        if ocr_pdf is not None:
            ocr_pdf.close()
    return _result("pdf", snippets, warnings)


def _parse_docx(data: bytes) -> dict:
    _verify_ooxml(data, "word/document.xml")
    doc = Document(BytesIO(data))
    snippets = []
    warnings = []
    section = None
    paragraph_index = 0
    table_index = 0
    for block in doc.iter_inner_content():
        if isinstance(block, Paragraph):
            paragraph_index += 1
            style = block.style
            style_name = style.name if style else ""
            is_heading = bool(re.match(r"^(?:Heading|标题)\s*[1-9]$", style_name, re.IGNORECASE)
                              or block._p.xpath("./w:pPr/w:outlineLvl[@w:val!='9']")
                              or (style and style.element.xpath("./w:pPr/w:outlineLvl[@w:val!='9']")))
            if is_heading and block.text.strip():
                section = block.text.strip()
            _push_text(snippets, block.text, kind="heading" if is_heading else "paragraph",
                       locator=f"段落 {paragraph_index}", section=section)
        elif isinstance(block, Table):
            table_index += 1
            for row_index, row in enumerate(block.rows, 1):
                values = [cell.text for cell in row.cells]
                if any(value.strip() for value in values):
                    _push_text(snippets, " | ".join(values), kind="table_row", section=section,
                               locator=f"表 {table_index} 行 {row_index}", cells=values)
    if (doc.element.xpath(".//w:drawing | .//w:pict | .//w:txbxContent | .//w:object")
            or doc.element.xpath(".//w:tbl/w:tr/w:tc/w:tbl")):
        warnings.append("Word 正文含图片、文本框、嵌入对象或嵌套表格，请核对这些内容。")
    return _result("docx", snippets, warnings)


def _value_for_json(value):
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, timedelta):
        return value.total_seconds()
    return value


def _parse_xlsx(data: bytes) -> dict:
    _verify_ooxml(data, "xl/workbook.xml")
    formulas = load_workbook(BytesIO(data), read_only=True, data_only=False, keep_links=False)
    values = None
    snippets = []
    warnings = []
    total_cells = 0
    try:
        values = load_workbook(BytesIO(data), read_only=True, data_only=True, keep_links=False)
        for source in formulas.worksheets:
            cached = values[source.title]
            if source.max_row is None or source.max_column is None:
                warnings.append(f"工作表 {source.title} 缺少范围元数据，请在 Excel 中重新另存后解析。")
                continue
            total_cells += source.max_row * source.max_column
            if total_cells > MAX_SHEET_CELLS:
                raise FormatError("Excel 工作表累计扫描范围超过上限，请拆分文件。")
            missing_cache = 0
            for row_number, (raw_row, value_row) in enumerate(zip(source.iter_rows(), cached.iter_rows(), strict=True), 1):
                row_values = []
                for column, (raw, cached_value) in enumerate(zip(raw_row, value_row, strict=True), 1):
                    if raw.value is None:
                        continue
                    coordinate = f"{get_column_letter(column)}{row_number}"
                    entry = {"cell": coordinate, "value": _value_for_json(raw.value)}
                    if raw.data_type == "f":
                        entry = {"cell": coordinate,
                                 "formula": raw.value if isinstance(raw.value, str) else getattr(raw.value, "text", None),
                                 "value": _value_for_json(cached_value.value)}
                        if not isinstance(raw.value, str):
                            entry["formula_range"] = getattr(raw.value, "ref", None)
                        if cached_value.value is None:
                            missing_cache += 1
                    elif (type(raw.value) in (int, float) and 0 <= raw.value == int(raw.value)
                          and re.fullmatch(r"0{2,}", raw.number_format or "")):
                        entry["display"] = str(int(raw.value)).zfill(len(raw.number_format))
                    row_values.append(entry)
                if row_values:
                    first, last = row_values[0]["cell"], row_values[-1]["cell"]
                    address = f"{first}:{last}" if first != last else first
                    parts = [f"{entry['cell']}={entry.get('display', entry['value'])}"
                             if entry["value"] is not None else
                             f"{entry['cell']}={entry.get('formula') or '公式'}（无缓存值）" for entry in row_values]
                    _push_text(snippets, " | ".join(parts), kind="sheet_row", locator=f"{source.title}!{address}",
                               section=source.title, cells=row_values, sheet_state=source.sheet_state)
            if missing_cache:
                warnings.append(f"工作表 {source.title} 有 {missing_cache} 个公式没有缓存值，请复核。")
    finally:
        formulas.close()
        if values is not None:
            values.close()
    return _result("xlsx", snippets, warnings)


def _parse_txt(data: bytes) -> dict:
    encodings = ("utf-16",) if data.startswith((b"\xff\xfe", b"\xfe\xff")) else ("utf-8-sig", "gb18030")
    for encoding in encodings:
        try:
            text = data.decode(encoding)
        except UnicodeDecodeError:
            continue
        if "\x00" not in text:
            break
    else:
        raise FormatError("文本编码无法识别，请重新保存为 UTF-8。")
    snippets = []
    for index, line in enumerate(text.splitlines(), 1):
        _push_text(snippets, line, kind="paragraph", locator=f"第 {index} 行")
    return _result("txt", snippets, [])


def parse_document(filename: str, data: bytes) -> dict:
    """Return extraction status and excerpts with format-specific locations."""
    if (not isinstance(filename, str) or not filename.strip() or len(filename) > 255
            or re.search(r"[\\/:\x00-\x1f]", filename) or filename in {".", ".."}):
        raise FormatError("请输入单个文件名，请去掉路径和控制字符。")
    if not data or len(data) > MAX_UPLOAD_BYTES:
        raise FormatError("文件为空或超过 32 MiB，请核对或拆分文件。")
    kind = Path(filename).suffix.lower().lstrip(".")
    parser = {"pdf": _parse_pdf, "docx": _parse_docx, "xlsx": _parse_xlsx, "txt": _parse_txt}.get(kind)
    try:
        if parser:
            return parser(data)
        if kind in _IMAGE_TYPES:
            return _parse_image(kind, data)
    except FormatError:
        raise
    except Exception as exc:
        raise FormatError("文件解析失败。请检查是否已加密、格式是否正确或文件是否损坏。") from exc
    return _result(kind or "unknown", [], ["此格式需要专用工具。请补充可定位的 PDF、DOCX 或 XLSX 导出件。"],
                   status="unsupported")


def _safe_office_text(value: str) -> str:
    return _CONTROL_CHARACTERS.sub("", value)


def _write_cell(cell, value) -> None:
    if isinstance(value, (dict, list, tuple)):
        value = json.dumps(value, ensure_ascii=False, default=str)
    if isinstance(value, str):
        value = _safe_office_text(value)
        if len(value) > _CELL_TEXT_LIMIT:
            raise FormatError("导出单元格超过 Excel 文本长度限制。请拆分这一行的内容。")
        cell.value = value
        # Source quotes and IDs must stay literal text, even when starting with '='.
        cell.data_type = "s"
    else:
        cell.value = value


def render_matrix(rows: list[dict], title: str, fields: Sequence[tuple[str, str]] | None = None) -> bytes:
    """Render the requirements or scoring matrix without executable spreadsheet formulas."""
    if fields is None:
        keys = dict.fromkeys(key for row in rows for key in row)
        fields = [(key, key) for key in keys] or [("result", "结果")]
    if not fields:
        raise FormatError("导出矩阵至少需要一列。")
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = re.sub(r"[\\/*?:\[\]]", " ", _safe_office_text(title)).strip()[:31] or "矩阵"
    for index, (_, label) in enumerate(fields, 1):
        cell = sheet.cell(row=1, column=index)
        _write_cell(cell, label)
        cell.fill = PatternFill("solid", fgColor="25445E")
        cell.font = Font(name="等线", color="FFFFFF", bold=True)
        cell.alignment = Alignment(vertical="center", wrap_text=True)
    sheet.row_dimensions[1].height = 24
    widths = [max(12, len(label) * 2 + 2) for _, label in fields]
    for row_number, row in enumerate(rows, 2):
        for index, (key, _) in enumerate(fields, 1):
            cell = sheet.cell(row=row_number, column=index)
            _write_cell(cell, row.get(key))
            cell.alignment = Alignment(vertical="top", wrap_text=True)
            if isinstance(cell.value, str):
                widths[index - 1] = max(widths[index - 1], min(48, len(cell.value) + 2))
    for index, width in enumerate(widths, 1):
        sheet.column_dimensions[get_column_letter(index)].width = width
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = sheet.dimensions
    stream = BytesIO()
    workbook.save(stream)
    workbook.close()
    return stream.getvalue()


def _field(paragraph, instruction: str, placeholder: str) -> None:
    field = OxmlElement("w:fldSimple")
    field.set(qn("w:instr"), instruction)
    run = OxmlElement("w:r")
    text = OxmlElement("w:t")
    text.text = placeholder
    run.append(text)
    field.append(run)
    paragraph._p.append(field)


def _default_document():
    doc = Document()
    section = doc.sections[0]
    section.page_height = Cm(29.7)
    section.page_width = Cm(21)
    section.top_margin = Cm(2.5)
    section.bottom_margin = Cm(2.5)
    section.left_margin = Cm(2.6)
    section.right_margin = Cm(2.6)
    for name, size in (("Normal", 11), ("Title", 20), ("Heading 1", 16), ("Heading 2", 13)):
        style = doc.styles[name]
        style.font.name = "Noto Sans CJK SC"
        style.font.size = Pt(size)
        style.font.color.rgb = RGBColor(0, 0, 0)
        style.element.get_or_add_rPr().rFonts.set(qn("w:eastAsia"), "Noto Sans CJK SC")
    body = doc.styles["Normal"].paragraph_format
    body.line_spacing = 1.35
    body.space_after = Pt(6)
    footer = section.footer.paragraphs[0]
    footer.alignment = WD_ALIGN_PARAGRAPH.CENTER
    _field(footer, "PAGE", "1")
    return doc


def _add_markdown(doc, content: str) -> None:
    pending_table = []

    def flush_table():
        if not pending_table:
            return
        width = max(map(len, pending_table))
        table = doc.add_table(rows=0, cols=width)
        table.style = "Table Grid"
        for values in pending_table:
            cells = table.add_row().cells
            for cell, value in zip(cells, values):
                cell.text = value
        pending_table.clear()

    for line in content.splitlines():
        line = _safe_office_text(line).strip()
        if line.startswith("|") and line.endswith("|") and line.count("|") >= 3:
            cells = [value.strip().replace(r"\|", "|") for value in re.split(r"(?<!\\)\|", line[1:-1])]
            if not all(re.fullmatch(r":?-+:?", value) for value in cells):
                pending_table.append(cells)
            continue
        flush_table()
        if not line:
            continue
        heading = re.match(r"^#{1,6}\s+(.+)$", line)
        if heading:
            doc.add_heading(heading.group(1), level=2)
        elif re.match(r"^[-*+]\s+", line):
            doc.add_paragraph(line[2:], style="List Bullet")
        else:
            doc.add_paragraph(line)
    flush_table()


def render_docx(title: str, chapters: list[dict], *, template: bytes | None = None) -> bytes:
    """Use a plain review template or insert content at a supplied Word marker."""
    marker = None
    if template is None:
        doc = _default_document()
    else:
        _verify_ooxml(template, "word/document.xml")
        doc = Document(BytesIO(template))
        matches = [paragraph for paragraph in doc.paragraphs if paragraph.text.strip() == "{{BID_BODY}}"]
        if len(matches) != 1:
            raise FormatError("Word 模板应在正文中包含一个独立的 {{BID_BODY}} 占位段落。")
        marker = matches[0]._p

    existing = set(doc.element.body)
    doc.add_paragraph(_safe_office_text(title), style="Title")
    if len(chapters) > 1:
        if template is None:
            doc.add_page_break()
        toc_heading = doc.add_paragraph("目录", style="Subtitle")
        toc_heading.paragraph_format.keep_with_next = True
        _field(doc.add_paragraph(), 'TOC \\o "1-2" \\h \\z \\u', "在 Word 中更新目录域以显示页码。")
        settings = doc.settings.element
        changes = settings.find(qn("w:updateFields"))
        if changes is None:
            changes = OxmlElement("w:updateFields")
            settings.append(changes)
        changes.set(qn("w:val"), "true")
        doc.add_page_break()

    for index, chapter in enumerate(chapters, 1):
        label = chapter.get("chapter_name") or chapter.get("title") or f"第 {index} 章"
        doc.add_heading(_safe_office_text(str(label)), level=1)
        content = str(chapter.get("content") or "")
        if not content.strip():
            content = "【待确认：章节正文】"
        _add_markdown(doc, content)

    if marker is not None:
        for element in list(doc.element.body):
            if element not in existing:
                marker.addprevious(element)
        marker.getparent().remove(marker)
    stream = BytesIO()
    doc.save(stream)
    return stream.getvalue()


def render_pdf(docx_data: bytes) -> bytes:
    """Convert the draft using an explicitly configured, local LibreOffice binary."""
    binary = os.environ.get("BIAOSHU_SOFFICE_BIN")
    if not binary:
        raise FormatError("未配置本地 LibreOffice PDF 转换。可直接下载 DOCX。")
    _verify_ooxml(docx_data, "word/document.xml")
    # The converter must not resolve a template's linked assets or dynamic fields.
    safe_fields = {"PAGE", "NUMPAGES", "TOC", "SECTION", "SECTIONPAGES", "REF", "PAGEREF", "STYLEREF"}
    try:
        with ZipFile(BytesIO(docx_data)) as archive:
            for name in archive.namelist():
                if name.lower().endswith(".bin") or name.startswith("word/embeddings/"):
                    raise FormatError("Word 含宏或嵌入对象，需要人工转换 PDF。")
                if name.endswith(".rels"):
                    links = ElementTree.fromstring(archive.read(name))
                    if any(link.get("TargetMode", "").lower() == "external" for link in links):
                        raise FormatError("Word 含外部链接，需要人工转换 PDF。")
                if name.startswith("word/") and name.endswith(".xml"):
                    content = ElementTree.fromstring(archive.read(name))
                    if content.findall(f".//{qn('w:altChunk')}"):
                        raise FormatError("Word 含可导入的外部格式内容，需要人工转换 PDF。")
                    # Complex Word fields may span runs, so reject them without guessing the instruction.
                    if content.findall(f".//{qn('w:instrText')}"):
                        raise FormatError("Word 含复杂域，需要人工转换 PDF。")
                    for field in content.findall(f".//{qn('w:fldSimple')}"):
                        instruction = field.get(qn("w:instr"), "").strip().split()
                        if not instruction or instruction[0].upper() not in safe_fields:
                            raise FormatError("Word 含需额外处理的域，需要人工转换 PDF。")
    except ElementTree.ParseError as exc:
        raise FormatError("无法检查 Word 包中的外部链接或域。") from exc
    with TemporaryDirectory(prefix="biaoshu-pdf-") as directory:
        root = Path(directory)
        source = root / "draft.docx"
        source.write_bytes(docx_data)
        try:
            subprocess.run([binary, f"-env:UserInstallation={(root / 'profile').as_uri()}", "--headless",
                            "--convert-to", "pdf", "--outdir", str(root), str(source)], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, check=True, timeout=120)
            result = (root / "draft.pdf").read_bytes()
        except (OSError, subprocess.TimeoutExpired, subprocess.CalledProcessError) as exc:
            raise FormatError("本地 DOCX 转 PDF 失败。可直接下载 DOCX。") from exc
        if not result.startswith(b"%PDF-"):
            raise FormatError("本地转换未生成有效 PDF。")
        return result
