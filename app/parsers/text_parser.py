"""纯文本类解析器：.txt / .md / .json。

Markdown 按 `#` 层级产出标题块（结构感知分块可直接用）；
JSON 递归展平为「路径: 值」并生成语义化句子，便于检索键值含义。
"""
import json
import logging
import re

from app.parsers.base import Block, ParsedDocument, decode_text, flatten_json

logger = logging.getLogger(__name__)

_MD_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_MANUAL_HEADING_RE = re.compile(r"^(第[一二三四五六七八九十百千]+[章节篇条]|[0-9]+(\.[0-9]+){0,3}[、.\s])")


class TextParser:
    """纯文本 / Markdown。"""

    name = "text"
    extensions = (".txt", ".md", ".markdown")

    def parse(self, content: bytes, filename: str) -> ParsedDocument:
        text, encoding = decode_text(content)
        blocks: list[Block] = []
        for raw in text.splitlines():
            line = raw.rstrip()
            if not line.strip():
                continue
            m = _MD_HEADING_RE.match(line)
            if m:
                blocks.append(Block(type="title", text=m.group(2).strip(), level=len(m.group(1))))
            elif len(line.strip()) <= 40 and _MANUAL_HEADING_RE.match(line.strip()):
                blocks.append(Block(type="title", text=line.strip(), level=1))
            else:
                blocks.append(Block(type="paragraph", text=line.strip()))

        return ParsedDocument(
            text=text,
            blocks=blocks,
            metadata={
                "format": "text",
                "encoding": encoding,
                "lines": len(text.splitlines()),
                "block_count": len(blocks),
            },
            warnings=[],
        )


class JsonParser:
    """JSON → 展平的键值文本。"""

    name = "json"
    extensions = (".json",)

    def parse(self, content: bytes, filename: str) -> ParsedDocument:
        text, encoding = decode_text(content)
        warnings: list[str] = []
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            warnings.append(f"JSON 解析失败（{exc}），按纯文本处理")
            return TextParser().parse(content, filename)

        flat, semantic = flatten_json(data)
        blocks = [Block(type="paragraph", text=flat, meta={"semantic": semantic})]
        return ParsedDocument(
            text=flat,
            blocks=blocks,
            metadata={"format": "json", "encoding": encoding, "block_count": len(blocks)},
            warnings=warnings,
        )


__all__ = ["TextParser", "JsonParser"]
