"""解析层契约：统一数据模型 + 解析器基类 + 通用工具。

设计意图（为什么要有这一层）：
- **统一契约**：所有格式都产出 `ParsedDocument`，下游（分块 / 索引 / 检索）完全不感知格式差异，
  避免「每加一种格式就在业务代码里加一个 if 分支」的扩散式修改；
- **保留结构**：`Block` 保留标题层级、表格、页码、sheet 名——
  这些结构信息是「结构感知分块」的前提（原版只返回纯字符串，结构在解析阶段就丢了）；
- **表格双写**：表格同时产出 Markdown（给人看/答案展示）与语义化句子（给检索用，
  因为「| 一线城市 | 500 元 |」这种表格在向量/BM25 检索里语义是残缺的）。
"""
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

BlockType = Literal["title", "paragraph", "table", "image"]


class ParseError(ValueError):
    """解析失败（文件损坏、格式不符、加密文档等）。

    单独定义异常类型，是为了让上层能区分：
    - 客户端问题（ParseError）→ HTTP 400，提示用户换文件；
    - 服务端问题（模型调用失败等）→ HTTP 502。
    """


# ======================================================================
# 数据模型
# ======================================================================
@dataclass
class Block:
    """结构化内容块。"""

    type: BlockType
    text: str
    level: int = 0                 # 标题层级（type == "title" 时有效）
    page: int | None = None        # PDF 页码（从 1 开始）
    sheet: str | None = None       # Excel sheet 名
    meta: dict[str, Any] = field(default_factory=dict)  # 如 {"semantic": [...], "ocr": True}

    def to_dict(self) -> dict:
        return {
            "type": self.type,
            "text": self.text,
            "level": self.level,
            "page": self.page,
            "sheet": self.sheet,
            "meta": self.meta,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Block":
        return cls(
            type=data.get("type", "paragraph"),
            text=data.get("text", ""),
            level=int(data.get("level", 0) or 0),
            page=data.get("page"),
            sheet=data.get("sheet"),
            meta=data.get("meta") or {},
        )

    @property
    def retrieval_text(self) -> str:
        """用于检索的文本：表格额外附上语义化句子。"""
        semantic = self.meta.get("semantic") or []
        if semantic:
            return self.text + "\n" + "\n".join(semantic)
        return self.text


@dataclass
class ParsedDocument:
    """统一解析结果。"""

    text: str                       # 全文（兜底/无结构场景）
    blocks: list[Block] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "text": self.text,
            "blocks": [b.to_dict() for b in self.blocks],
            "metadata": self.metadata,
            "warnings": self.warnings,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ParsedDocument":
        return cls(
            text=data.get("text", ""),
            blocks=[Block.from_dict(b) for b in data.get("blocks", [])],
            metadata=data.get("metadata") or {},
            warnings=list(data.get("warnings") or []),
        )

    @property
    def is_empty(self) -> bool:
        return not self.text.strip() and not self.blocks


class BaseParser(Protocol):
    """解析器契约：新增格式只需实现本协议并注册。"""

    name: str
    extensions: tuple[str, ...]

    def parse(self, content: bytes, filename: str) -> ParsedDocument:  # pragma: no cover - 协议定义
        ...

    def needs_async(self, content: bytes, filename: str) -> bool:
        """是否需要异步处理（耗时操作：OCR / 大文件）。"""
        return False


# ======================================================================
# 通用工具
# ======================================================================
_ENCODINGS = ("utf-8", "utf-8-sig", "gbk", "gb18030")


def decode_text(content: bytes) -> tuple[str, str]:
    """多编码回退解码，返回 (文本, 使用的编码)。"""
    for enc in _ENCODINGS:
        try:
            return content.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return content.decode("utf-8", errors="ignore"), "utf-8(ignore)"


def rows_to_markdown(
    rows: list[list[str]],
    table_name: str = "",
    max_cols: int = 15,
    max_rows: int = 300,
) -> tuple[str, list[str], list[str]]:
    """二维表 → (Markdown 文本, 语义化句子列表, 告警列表)。

    语义化示例：
        输入: [["城市级别", "住宿标准"], ["一线城市", "500 元/晚"]]
        输出: ["差旅住宿标准：城市级别 为 一线城市，住宿标准 为 500 元/晚。"]
    原理：把「行列值」还原成带主谓的自然语言，让向量/BM25 能命中口语化提问。
    """
    warnings: list[str] = []
    rows = [[("" if c is None else str(c).strip()) for c in r] for r in rows]
    rows = [r for r in rows if any(c for c in r)]
    if not rows:
        return "", [], warnings

    if len(rows) > max_rows:
        warnings.append(f"表格行数 {len(rows)} 超过上限 {max_rows}，已截断")
        rows = rows[:max_rows]

    width = max(len(r) for r in rows)
    if width > max_cols:
        warnings.append(f"表格列数 {width} 超过上限 {max_cols}，已截断")
        width = max_cols

    norm = [(r + [""] * width)[:width] for r in rows]
    header, body = norm[0], norm[1:]

    # Markdown（展示用）
    md_lines = [
        "| " + " | ".join(h or f"列{i + 1}" for i, h in enumerate(header)) + " |",
        "| " + " | ".join("---" for _ in range(width)) + " |",
    ]
    for row in body:
        md_lines.append("| " + " | ".join(row) + " |")

    # 语义化句子（检索用）
    title = table_name or "表格"
    semantic: list[str] = []
    for row in body:
        pairs = [f"{header[i] or f'列{i + 1}'} 为 {row[i]}" for i in range(width) if row[i]]
        if pairs:
            semantic.append(f"{title}：" + "，".join(pairs) + "。")

    return "\n".join(md_lines), semantic, warnings


def flatten_json(data: Any, prefix: str = "", max_items: int = 500) -> tuple[str, list[str]]:
    """JSON → (缩进文本, 语义化句子列表)。"""
    lines: list[str] = []
    semantic: list[str] = []

    def walk(node: Any, path: str) -> None:
        if len(semantic) >= max_items:
            return
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, f"{path}.{k}" if path else str(k))
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, f"{path}[{i}]")
        else:
            text = "" if node is None else str(node)
            lines.append(f"{path}: {text}")
            semantic.append(f"{path} 为 {text}。")

    walk(data, prefix)
    return "\n".join(lines), semantic


__all__ = [
    "Block",
    "ParsedDocument",
    "BaseParser",
    "ParseError",
    "decode_text",
    "rows_to_markdown",
    "flatten_json",
]
