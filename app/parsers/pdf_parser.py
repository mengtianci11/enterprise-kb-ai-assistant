"""PDF 解析器：文本层优先，扫描件自动回落 OCR。

两步走的原因（成本-精度权衡）：
1. 先用 pypdf 探测每页字符数与图像覆盖比例，判定是否为扫描件；
   文本层解析是毫秒级，OCR 是秒级（约慢 100 倍），先判定可避免无谓开销；
2. 判定为扫描件才用 PyMuPDF 渲染页面 → RapidOCR 识别。

为什么用 PyMuPDF 渲染：它是纯 wheel、自带渲染引擎，
不需要 poppler 等外部二进制（pdf2image 在 Windows 部署极麻烦）。
"""
import io
import logging
import re

import pypdf

from app.config import settings
from app.parsers import ocr as ocr_mod
from app.parsers.base import Block, ParsedDocument

logger = logging.getLogger(__name__)

# 轻量标题启发式：中文章节标题 / 数字编号标题 / Markdown 标题
_HEADING_RE = re.compile(r"^(第[一二三四五六七八九十百千]+[章节篇条]|[0-9]+(\.[0-9]+){0,3}[、.\s]|#{1,6}\s)")


def _open_pdf(content: bytes):
    """打开 PDF（优先 pymupdf，兼容旧版 fitz 命名）。"""
    try:
        import pymupdf  # PyMuPDF >= 1.24 推荐命名

        return pymupdf.open(stream=content, filetype="pdf")
    except ImportError:
        import fitz  # 旧命名

        return fitz.open(stream=content, filetype="pdf")


class PdfParser:
    """PDF 文本层 + 扫描件 OCR。"""

    name = "pdf"
    extensions = (".pdf",)

    # ------------------------------------------------------------------
    # 探测
    # ------------------------------------------------------------------
    def probe(self, content: bytes) -> dict:
        """探测 PDF 形态，返回页数/每页字符数/图像占比/是否扫描件。"""
        info = {"pages": 0, "text_per_page": 0.0, "image_ratio": 0.0, "is_scanned": False}
        try:
            reader = pypdf.PdfReader(io.BytesIO(content))
            pages = len(reader.pages)
            probe_pages = min(pages, max(settings.SCANNED_PROBE_PAGES, 1))
            text = "".join((reader.pages[i].extract_text() or "") for i in range(probe_pages))
            info["pages"] = pages
            info["text_per_page"] = len(text.strip()) / max(probe_pages, 1)
        except Exception as exc:  # noqa: BLE001
            logger.warning("PDF 文本层探测失败：%s", exc)
            return info

        info["image_ratio"] = self._image_ratio(content, settings.SCANNED_PROBE_PAGES)
        info["is_scanned"] = (
            info["text_per_page"] < settings.SCANNED_TEXT_PER_PAGE_MIN
            or info["image_ratio"] > settings.SCANNED_IMAGE_RATIO_MAX
        )
        return info

    @staticmethod
    def _image_ratio(content: bytes, max_pages: int) -> float:
        """估算前若干页的图像覆盖面积占比。"""
        try:
            doc = _open_pdf(content)
        except Exception:  # noqa: BLE001
            return 0.0
        try:
            total_ratio, counted = 0.0, 0
            for i in range(min(doc.page_count, max(max_pages, 1))):
                page = doc[i]
                page_area = abs(page.rect.width * page.rect.height) or 1.0
                img_area = 0.0
                for item in page.get_image_info():
                    x0, y0, x1, y1 = item["bbox"]
                    img_area += abs((x1 - x0) * (y1 - y0))
                total_ratio += min(img_area / page_area, 1.0)
                counted += 1
            return total_ratio / counted if counted else 0.0
        finally:
            doc.close()

    def needs_async(self, content: bytes, filename: str) -> bool:
        """扫描件需要 OCR（秒级），走异步。"""
        if not settings.OCR_ENABLED:
            return False
        return self.probe(content)["is_scanned"]

    # ------------------------------------------------------------------
    # 解析
    # ------------------------------------------------------------------
    def parse(self, content: bytes, filename: str) -> ParsedDocument:
        info = self.probe(content)
        if info["is_scanned"]:
            if settings.OCR_ENABLED and ocr_mod.ocr_available():
                return self._parse_by_ocr(content, filename, info)
            warn = "文档判定为扫描件，但 OCR 未启用或不可用，仅返回文本层可提取内容"
            logger.warning("%s：%s", filename, warn)
            parsed = self._parse_text_layer(content, filename, info)
            parsed.warnings.append(warn)
            return parsed
        return self._parse_text_layer(content, filename, info)

    def _parse_text_layer(self, content: bytes, filename: str, info: dict) -> ParsedDocument:
        reader = pypdf.PdfReader(io.BytesIO(content))
        blocks: list[Block] = []
        warnings: list[str] = []
        for page_no, page in enumerate(reader.pages, start=1):
            raw = page.extract_text() or ""
            for para in self._split_paragraphs(raw):
                if _HEADING_RE.match(para) and len(para) <= 40:
                    blocks.append(Block(type="title", text=para, level=1, page=page_no))
                else:
                    blocks.append(Block(type="paragraph", text=para, page=page_no))
        if not blocks:
            warnings.append("PDF 文本层为空（可能是纯图片扫描件），建议启用 OCR")
        text = "\n".join(b.text for b in blocks)
        return ParsedDocument(
            text=text,
            blocks=blocks,
            metadata={
                "format": "pdf",
                "pages": info.get("pages", len(reader.pages)),
                "is_scanned": False,
                "ocr_used": False,
                "block_count": len(blocks),
            },
            warnings=warnings,
        )

    def _parse_by_ocr(self, content: bytes, filename: str, info: dict) -> ParsedDocument:
        """渲染每页为图像后 OCR。"""
        doc = _open_pdf(content)
        blocks: list[Block] = []
        warnings: list[str] = []
        try:
            for page_no in range(doc.page_count):
                page = doc[page_no]
                pix = page.get_pixmap(dpi=settings.OCR_DPI)
                img_bytes = pix.tobytes("png")
                text, warns = ocr_mod.recognize(img_bytes, settings.OCR_MIN_CONFIDENCE)
                warnings.extend(f"第 {page_no + 1} 页：{w}" for w in warns)
                for para in self._split_paragraphs(text):
                    blocks.append(
                        Block(type="paragraph", text=para, page=page_no + 1, meta={"ocr": True})
                    )
        finally:
            doc.close()

        if not blocks:
            warnings.append("OCR 未从该 PDF 中识别出任何文字")
        text = "\n".join(b.text for b in blocks)
        return ParsedDocument(
            text=text,
            blocks=blocks,
            metadata={
                "format": "pdf",
                "pages": info.get("pages", 0),
                "is_scanned": True,
                "ocr_used": True,
                "block_count": len(blocks),
            },
            warnings=warnings,
        )

    @staticmethod
    def _split_paragraphs(raw: str) -> list[str]:
        """按空行/换行切段并清理空白。"""
        if not raw:
            return []
        parts = [p.strip() for p in re.split(r"\n\s*\n", raw)]
        if len(parts) <= 1:  # 无空行时退化为按行
            parts = [p.strip() for p in raw.splitlines()]
        return [p for p in parts if p]


__all__ = ["PdfParser"]
