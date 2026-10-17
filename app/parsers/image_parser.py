"""图片解析器：直接 OCR（.png/.jpg/.jpeg/.bmp/.tif/.tiff/.webp）。

图片必然需要 OCR（秒级），因此 `needs_async()` 恒为 True（在 OCR 可用时），
交给后台任务处理，避免阻塞上传接口。
"""
import logging

from app.config import settings
from app.parsers import ocr as ocr_mod
from app.parsers.base import Block, ParsedDocument

logger = logging.getLogger(__name__)


class ImageParser:
    """图片 OCR 解析。"""

    name = "image"
    extensions = (".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp")

    def needs_async(self, content: bytes, filename: str) -> bool:
        return bool(settings.OCR_ENABLED and ocr_mod.ocr_available())

    def parse(self, content: bytes, filename: str) -> ParsedDocument:
        if not settings.OCR_ENABLED:
            return ParsedDocument(
                text="",
                blocks=[],
                metadata={"format": "image", "ocr_used": False},
                warnings=["OCR 已禁用（OCR_ENABLED=false），无法从图片提取文字"],
            )
        if not ocr_mod.ocr_available():
            return ParsedDocument(
                text="",
                blocks=[],
                metadata={"format": "image", "ocr_used": False},
                warnings=["OCR 组件不可用，请安装 rapidocr-onnxruntime"],
            )

        text, warnings = ocr_mod.recognize(content, settings.OCR_MIN_CONFIDENCE)
        blocks = [
            Block(type="paragraph", text=line.strip(), meta={"ocr": True})
            for line in text.splitlines()
            if line.strip()
        ]
        if not blocks:
            warnings.append("图片中未识别到文字")
        return ParsedDocument(
            text="\n".join(b.text for b in blocks),
            blocks=blocks,
            metadata={
                "format": "image",
                "ocr_used": True,
                "block_count": len(blocks),
            },
            warnings=warnings,
        )


__all__ = ["ImageParser"]
