"""Excel（.xlsx）解析器：按 sheet 建块 + 表格语义化。

选型：openpyxl（read_only + data_only）
- 原理：直接读 OOXML 的 sharedStrings/sheetN.xml，支持流式读取；
- 优势：保留 sheet 名与合并单元格（合并单元格的从属格自动填充主格值），
  大表内存占用可控；
- 对比：pandas.read_excel 会丢掉 sheet 语义、大表吃内存；xlrd 已不支持 xlsx。

为什么表格要做语义化：Excel 是「行列结构」，直接拍平成文本后
「产品A 库存 120」这类关系会丢失主语，向量检索几乎命中不了。
"""
import logging

from openpyxl import load_workbook

from app.config import settings
from app.parsers.base import Block, ParsedDocument, rows_to_markdown

logger = logging.getLogger(__name__)


class ExcelParser:
    """Excel 工作簿解析。"""

    name = "xlsx"
    extensions = (".xlsx",)

    def parse(self, content: bytes, filename: str) -> ParsedDocument:
        import io

        wb = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
        blocks: list[Block] = []
        warnings: list[str] = []
        sheets: list[str] = []
        table_count = 0

        try:
            for ws in wb.worksheets:
                rows = self._read_rows(ws)
                if not rows:
                    continue
                sheets.append(ws.title)
                table_count += 1
                md, semantic, warns = rows_to_markdown(
                    rows,
                    table_name=f"{ws.title} 表",
                    max_cols=settings.TABLE_MAX_COLS,
                    max_rows=settings.TABLE_MAX_ROWS,
                )
                warnings.extend(f"[{ws.title}] {w}" for w in warns)
                if md:
                    blocks.append(
                        Block(
                            type="table",
                            text=f"# {ws.title}\n{md}",
                            sheet=ws.title,
                            meta={"semantic": semantic, "rows": len(rows)},
                        )
                    )
        finally:
            wb.close()

        if not blocks:
            warnings.append("Excel 中未解析到有效数据（可能全是空表）")

        text = "\n".join(b.retrieval_text for b in blocks)
        return ParsedDocument(
            text=text,
            blocks=blocks,
            metadata={
                "format": "xlsx",
                "sheets": sheets,
                "sheet_count": len(sheets),
                "tables": table_count,
                "block_count": len(blocks),
            },
            warnings=warnings,
        )

    @staticmethod
    def _read_rows(ws) -> list[list[str]]:
        """读取单个 sheet 为二维字符串表，并做合并单元格填充。"""
        merged_values = _merged_cell_map(ws)
        rows: list[list[str]] = []
        max_rows = settings.TABLE_MAX_ROWS
        for idx, row in enumerate(ws.iter_rows(values_only=False), start=1):
            if idx > max_rows:
                break
            values: list[str] = []
            for cell in row:
                value = cell.value
                if value is None:
                    value = merged_values.get(cell.coordinate)
                values.append("" if value is None else str(value).strip())
            rows.append(values)
        # 去除右侧全空列与全空行
        while rows and not any(c for c in rows[-1]):
            rows.pop()
        return [r for r in rows if any(c for c in r)]


def _merged_cell_map(ws) -> dict:
    """合并单元格 → 用主格值填充区域内所有格。"""
    mapping: dict[str, str] = {}
    try:
        ranges = list(ws.merged_cells.ranges)
    except Exception:  # noqa: BLE001  (read_only 模式下可能不可用)
        return mapping
    for rng in ranges:
        try:
            anchor = ws.cell(row=rng.min_row, column=rng.min_col).value
        except Exception:  # noqa: BLE001
            continue
        if anchor is None:
            continue
        for r in range(rng.min_row, rng.max_row + 1):
            for c in range(rng.min_col, rng.max_col + 1):
                mapping[ws.cell(row=r, column=c).coordinate] = str(anchor).strip()
    return mapping


__all__ = ["ExcelParser"]
