"""HTML 解析器：保留标题层级、列表与表格。

选型：beautifulsoup4（html.parser 内置解析器，无需 lxml）
- 相比正则去标签：能正确识别 h1-h6 层级、table 结构、li 列表，
  这些结构对「结构感知分块」和检索质量都有直接价值。
"""
import logging

from bs4 import BeautifulSoup

from app.config import settings
from app.parsers.base import Block, ParsedDocument, decode_text, rows_to_markdown

logger = logging.getLogger(__name__)

_HEADING_TAGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}


class HtmlParser:
    """HTML 文档解析。"""

    name = "html"
    extensions = (".html", ".htm")

    def parse(self, content: bytes, filename: str) -> ParsedDocument:
        text, encoding = decode_text(content)
        soup = BeautifulSoup(text, "html.parser")

        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()

        blocks: list[Block] = []
        warnings: list[str] = []
        title_text = soup.title.get_text(strip=True) if soup.title else ""

        body = soup.body or soup
        for el in body.find_all(["h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "table", "pre"]):
            if el.name in _HEADING_TAGS:
                heading = el.get_text(" ", strip=True)
                if heading:
                    blocks.append(Block(type="title", text=heading, level=_HEADING_TAGS[el.name]))
            elif el.name == "table":
                rows = []
                for tr in el.find_all("tr"):
                    cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
                    if cells:
                        rows.append(cells)
                if not rows:
                    continue
                md, semantic, warns = rows_to_markdown(
                    rows,
                    table_name=title_text or filename,
                    max_cols=settings.TABLE_MAX_COLS,
                    max_rows=settings.TABLE_MAX_ROWS,
                )
                warnings.extend(warns)
                if md:
                    blocks.append(Block(type="table", text=md, meta={"semantic": semantic}))
            else:
                txt = el.get_text(" ", strip=True)
                if not txt or any(txt == b.text for b in blocks[-1:]):
                    continue
                blocks.append(Block(type="paragraph", text=txt))

        # 去重（嵌套元素会重复取到相同文本）
        seen: set[str] = set()
        deduped: list[Block] = []
        for b in blocks:
            key = f"{b.type}:{b.text}"
            if key in seen:
                continue
            seen.add(key)
            deduped.append(b)

        if not deduped:
            warnings.append("HTML 中未解析到有效文本")

        return ParsedDocument(
            text="\n".join(b.retrieval_text for b in deduped),
            blocks=deduped,
            metadata={
                "format": "html",
                "encoding": encoding,
                "title": title_text,
                "block_count": len(deduped),
            },
            warnings=warnings,
        )


__all__ = ["HtmlParser"]
