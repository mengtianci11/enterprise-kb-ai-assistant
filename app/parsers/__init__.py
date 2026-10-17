"""解析子系统统一入口：注册表 + 格式分发 + 解析缓存。

设计意图：
- **注册表模式**：格式与解析逻辑的映射是一份数据（PARSERS 列表），
  新增格式 = 新增解析器类并注册，业务代码零改动（开闭原则）；
- **白名单单一来源**：`supported_extensions()` 直接由注册表产出，
  路由层不再单独维护一份硬编码列表（消除双份维护导致的不一致）；
- **解析缓存**：按文件内容 MD5 缓存解析结果（`data/parsed/{md5}.json`），
  同一文件重复上传时解析成本约为 0（对 OCR 场景收益最大）。
"""
import hashlib
import json
import logging
from pathlib import Path

from app.config import settings
from app.parsers.base import Block, ParsedDocument, ParseError
from app.parsers.csv_parser import CsvParser
from app.parsers.docx_parser import DocxParser
from app.parsers.excel_parser import ExcelParser
from app.parsers.html_parser import HtmlParser
from app.parsers.image_parser import ImageParser
from app.parsers.pdf_parser import PdfParser
from app.parsers.pptx_parser import PptxParser
from app.parsers.text_parser import JsonParser, TextParser

logger = logging.getLogger(__name__)


class UnsupportedFormatError(ValueError):
    """不支持的文件格式。"""


# 注册表：顺序即匹配优先级（扩展名唯一，无冲突）
PARSERS = [
    PdfParser(),
    DocxParser(),
    ExcelParser(),
    PptxParser(),
    CsvParser(),
    HtmlParser(),
    JsonParser(),
    TextParser(),
    ImageParser(),
]

_EXT_MAP: dict[str, object] = {}
for _p in PARSERS:
    for _ext in _p.extensions:
        _EXT_MAP[_ext] = _p


# ======================================================================
# 对外 API
# ======================================================================
def supported_extensions() -> set[str]:
    """当前支持的扩展名集合（供路由白名单动态生成）。"""
    return set(_EXT_MAP.keys())


def parser_for(filename: str):
    """按文件扩展名取解析器。"""
    ext = Path(filename).suffix.lower()
    parser = _EXT_MAP.get(ext)
    if parser is None:
        raise UnsupportedFormatError(
            f"不支持的文件类型 {ext or '(无扩展名)'}，当前支持：{', '.join(sorted(_EXT_MAP))}"
        )
    return parser


def content_md5(content: bytes) -> str:
    return hashlib.md5(content).hexdigest()


def cache_path(md5: str) -> Path:
    return settings.PARSE_CACHE_DIR / f"{md5}.json"


def load_cached(md5: str) -> ParsedDocument | None:
    path = cache_path(md5)
    if not path.exists():
        return None
    try:
        return ParsedDocument.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except Exception as exc:  # noqa: BLE001
        logger.warning("解析缓存损坏（%s），将重新解析：%s", md5[:8], exc)
        return None


def save_cache(md5: str, parsed: ParsedDocument) -> None:
    try:
        settings.PARSE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache_path(md5).write_text(
            json.dumps(parsed.to_dict(), ensure_ascii=False), encoding="utf-8"
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("解析缓存写入失败：%s", exc)


def needs_async(content: bytes, filename: str) -> bool:
    """是否需要异步处理（OCR / 图片等耗时操作）。

    解析器按需覆写 `needs_async`，未覆写则视为同步（默认 False）——
    用 getattr 取默认值，避免解析器被迫实现全部协议方法。
    """
    try:
        parser = parser_for(filename)
    except UnsupportedFormatError:
        return False
    handler = getattr(parser, "needs_async", None)
    if handler is None:
        return False
    return bool(handler(content, filename))


def parse_document(
    content: bytes,
    filename: str,
    *,
    md5: str | None = None,
    use_cache: bool = True,
) -> tuple[ParsedDocument, bool]:
    """解析文档。

    返回 (解析结果, 是否命中缓存)。
    """
    md5 = md5 or content_md5(content)

    if use_cache:
        cached = load_cached(md5)
        if cached is not None:
            logger.info("解析缓存命中：%s", filename)
            return cached, True

    parser = parser_for(filename)
    try:
        parsed = parser.parse(content, filename)
    except ParseError:
        raise
    except Exception as exc:  # noqa: BLE001
        # 统一包装为 ParseError：文件损坏/格式不符属客户端问题（HTTP 400），
        # 不应表现为服务端 502，也不该把底层堆栈直接暴露给用户
        logger.warning("解析失败 %s：%s", filename, exc)
        raise ParseError(f"文档解析失败（文件可能损坏、格式不符或已加密）：{exc}") from exc

    if use_cache:
        save_cache(md5, parsed)
    return parsed, False


__all__ = [
    "Block",
    "ParsedDocument",
    "PARSERS",
    "UnsupportedFormatError",
    "ParseError",
    "supported_extensions",
    "parser_for",
    "parse_document",
    "needs_async",
    "content_md5",
    "load_cached",
    "save_cache",
]
