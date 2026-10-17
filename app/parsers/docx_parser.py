"""Word（.docx）解析器：保留标题层级与表格结构。

选型：python-docx
- 原理：直接读取 OOXML（word/document.xml + styles），可拿到段落样式与表格对象；
- 优势：标题样式可映射为层级（结构感知分块的关键），表格可完整取到行列；
- 对比：docx2txt 丢表格、mammoth 转 HTML 会丢层级语义、unstructured 依赖过重。

注意：遍历 body 子元素（而非 doc.paragraphs / doc.tables 分开遍历），
这样才能保持「段落与表格在文中的真实先后顺序」。
"""
import io
import logging
import re
from typing import Iterator

from docx import Document
from docx.table import Table
from docx.text.paragraph import Paragraph

from app.config import settings
from app.parsers.base import Block, ParsedDocument, rows_to_markdown

logger = logging.getLogger(__name__)

_HEADING_STYLE_RE = re.compile(r"(?:heading|标题)\s*(\d)", re.IGNORECASE)
_MANUAL_HEADING_RE = re.compile(r"^(第[一二三四五六七八九十百千]+[章节篇条]|[0-9]+(\.[0-9]+){0,3}[、.\s])")


def _iter_block_items(doc) -> Iterator[Paragraph | Table]:
    """按文档顺序遍历段落与表格。"""
    body = doc.element.body
    for child in body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            yield Paragraph(child, doc)
        elif tag == "tbl":
            yield Table(child, doc)


class DocxParser:
    """Word 文档解析。"""

    name = "docx"
    extensions = (".docx",)

    def parse(self, content: bytes, filename: str) -> ParsedDocument:
        doc = Document(io.BytesIO(content))
        blocks: list[Block] = []
        warnings: list[str] = []
        section_path: list[str] = []
        heading_count = table_count = 0

        for item in _iter_block_items(doc):
            if isinstance(item, Paragraph):
                text = (item.text or "").strip()
                if not text:
                    continue
                level = self._heading_level(item)
                if level:
                    heading_count += 1
                    # 维护章节栈：同级或更高级标题出现时截断更深层级
                    section_path = section_path[: level - 1]
                    while len(section_path) < level - 1:
                        section_path.append("")
                    section_path.append(text)
                    blocks.append(Block(type="title", text=text, level=level))
                else:
                    blocks.append(Block(type="paragraph", text=text))
            else:  # Table
                rows = [
                    [cell.text.strip() for cell in row.cells]
                    for row in item.rows
                ]
                if not rows:
                    continue
                table_count += 1
                name = " > ".join(p for p in section_path if p) or filename
                md, semantic, warns = rows_to_markdown(
                    rows, table_name=name, max_cols=settings.TABLE_MAX_COLS, max_rows=settings.TABLE_MAX_ROWS
                )
                warnings.extend(warns)
                if md:
                    blocks.append(Block(type="table", text=md, meta={"semantic": semantic}))

        if not blocks:
            warnings.append("Word 文档中未解析到文本内容")

        text = "\n".join(b.retrieval_text for b in blocks)
        return ParsedDocument(
            text=text,
            blocks=blocks,
            metadata={
                "format": "docx",
                "headings": heading_count,
                "tables": table_count,
                "block_count": len(blocks),
            },
            warnings=warnings,
        )

    @staticmethod
    def _heading_level(paragraph: Paragraph) -> int:
        """识别标题层级：优先样式，其次手工编号启发式。"""
        try:
            style_name = (paragraph.style.name or "") if paragraph.style else ""
        except Exception:  # noqa: BLE001
            style_name = ""
        m = _HEADING_STYLE_RE.search(style_name)
        if m:
            return max(1, min(int(m.group(1)), 6))
        text = (paragraph.text or "").strip()
        if len(text) <= 40 and _MANUAL_HEADING_RE.match(text):
            return 1
        return 0


__all__ = ["DocxParser"]
