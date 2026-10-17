"""CSV 解析器：复用表格语义化逻辑。"""
import csv
import io
import logging

from app.config import settings
from app.parsers.base import Block, ParsedDocument, decode_text, rows_to_markdown

logger = logging.getLogger(__name__)


class CsvParser:
    """CSV → 表格块（Markdown + 语义化句子）。"""

    name = "csv"
    extensions = (".csv", ".tsv")

    def parse(self, content: bytes, filename: str) -> ParsedDocument:
        text, encoding = decode_text(content)
        delimiter = "\t" if filename.lower().endswith(".tsv") else ","
        reader = csv.reader(io.StringIO(text), delimiter=delimiter)
        rows = [[c.strip() for c in row] for row in reader]
        rows = [r for r in rows if any(r)]

        warnings: list[str] = []
        if not rows:
            return ParsedDocument(
                text="",
                blocks=[],
                metadata={"format": "csv", "encoding": encoding, "rows": 0},
                warnings=["CSV 内容为空"],
            )

        md, semantic, warns = rows_to_markdown(
            rows,
            table_name=filename.rsplit(".", 1)[0],
            max_cols=settings.TABLE_MAX_COLS,
            max_rows=settings.TABLE_MAX_ROWS,
        )
        warnings.extend(warns)
        block = Block(type="table", text=md, meta={"semantic": semantic, "rows": len(rows)})
        return ParsedDocument(
            text=block.retrieval_text,
            blocks=[block],
            metadata={
                "format": "csv",
                "encoding": encoding,
                "rows": len(rows),
                "columns": len(rows[0]) if rows else 0,
                "block_count": 1,
            },
            warnings=warnings,
        )


__all__ = ["CsvParser"]
