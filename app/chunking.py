"""文本分块策略（RAG 检索质量的第一道关卡）。

- fixed: 固定滑窗切分（原版逻辑），实现简单、参数可复现；
- recursive: 段落感知的递归分块（推荐），先按空行聚合段落，
  再按 chunk_size 合并，超长段回落滑窗切分 —— 尽量保住语义完整，
  减少「一句话被拦腰截断」导致的检索噪声。
"""
import re
from typing import List

SEPARATORS = ["\n\n", "\n", "。"]


def split_text_fixed(text: str, chunk_size: int = 300, overlap: int = 50) -> List[str]:
    """固定窗口滑动切分。"""
    if chunk_size <= 0 or overlap < 0 or overlap >= chunk_size:
        raise ValueError("参数不合法：需满足 chunk_size > 0 且 0 <= overlap < chunk_size")
    chunks: List[str] = []
    start = 0
    while start < len(text):
        end = start + chunk_size
        chunks.append(text[start:end])
        start = end - overlap
    return [c for c in chunks if c.strip()]


def split_text_recursive(
    text: str, chunk_size: int = 300, overlap: int = 50, seps: List[str] | None = None
) -> List[str]:
    """段落感知递归分块：先按段落聚合，超长再滑窗。"""
    if chunk_size <= 0 or overlap < 0 or overlap >= chunk_size:
        raise ValueError("参数不合法：需满足 chunk_size > 0 且 0 <= overlap < chunk_size")
    seps = seps or SEPARATORS
    text = text.strip()
    if not text:
        return []

    # 1) 按段落切分（保留边界信息）
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]

    # 2) 逐步合并段落，超过容量则落块
    chunks: List[str] = []
    buffer = ""
    for para in paragraphs:
        if len(para) > chunk_size:
            # 超长段落：先落缓冲，再滑窗切该段
            if buffer:
                chunks.append(buffer)
                buffer = ""
            chunks.extend(split_text_fixed(para, chunk_size, overlap))
            continue
        if not buffer:
            buffer = para
        elif len(buffer) + 1 + len(para) <= chunk_size:
            buffer = f"{buffer}\n{para}"
        else:
            chunks.append(buffer)
            buffer = para
    if buffer:
        chunks.append(buffer)

    # 3) 可选：对块做轻量后处理，缝合过短的尾巴
    merged: List[str] = []
    for c in chunks:
        if merged and len(merged[-1]) + 1 + len(c) <= chunk_size:
            merged[-1] = f"{merged[-1]}\n{c}"
        else:
            merged.append(c)
    return [c.strip() for c in merged if c.strip()]


def split_text(  # noqa: D103
    text: str, strategy: str = "recursive", chunk_size: int = 300, overlap: int = 50
) -> List[str]:
    if strategy == "fixed":
        return split_text_fixed(text, chunk_size, overlap)
    return split_text_recursive(text, chunk_size, overlap)


# ======================================================================
# 结构感知分块（v3.0 新增）
# ======================================================================
def split_blocks(
    blocks: List[dict],
    doc_name: str = "",
    chunk_size: int = 300,
    overlap: int = 50,
    include_header: bool = True,
) -> List[dict]:
    """按文档结构（标题层级）切分，并在块内注入章节上下文。

    为什么这样做（原理）：
    1. **语义完整性**：定长滑窗会在任意位置截断，切出的块常常丢失主题；
       按标题边界聚合能把「同一节的完整论述」放进同一个块；
    2. **上下文补偿**：分块天然造成「语义孤岛」——「年假增加至10天」这一块
       不知道自己属于《员工手册》的「第三章 休假制度」。注入
       `[员工手册.md > 第三章 休假制度]` 后：
       - embedding 携带主题信息，语义更完整；
       - BM25 也能靠「休假制度」等章节词交叉命中；
       - 答案引用可精确到章节，企业场景可解释性更强；
    3. **表格优先独占**：表格块（含语义化句子）单独成块，避免被拆散。

    blocks: [{"type","text","level","meta":{...}}, ...]（来自解析层的 Block.to_dict）
    返回: [{"text","section","block_types","pages"}, ...]
    """
    if chunk_size <= 0 or overlap < 0 or overlap >= chunk_size:
        raise ValueError("参数不合法：需满足 chunk_size > 0 且 0 <= overlap < chunk_size")

    chunks: List[dict] = []
    section_stack: List[str] = []
    buffer: List[dict] = []
    buffer_len = 0

    def current_section() -> str:
        return " > ".join(s for s in section_stack if s)

    def make_text(body: str) -> str:
        if not include_header:
            return body
        section = current_section()
        prefix = f"[{doc_name} > {section}]" if section else (f"[{doc_name}]" if doc_name else "")
        return f"{prefix}\n{body}" if prefix else body

    def emit(body: str, block_types: List[str], pages: List[int]) -> None:
        body = body.strip()
        if not body:
            return
        chunks.append(
            {
                "text": make_text(body),
                "section": current_section(),
                "block_types": block_types,
                "pages": pages,
            }
        )

    def flush() -> None:
        nonlocal buffer, buffer_len
        if not buffer:
            return
        body = "\n".join(b["text"] for b in buffer)
        types = [b["type"] for b in buffer]
        pages = [b.get("page") for b in buffer if b.get("page")]
        # 超长缓冲再按尺寸切（此时已尽量贴近语义边界）
        if len(body) > chunk_size:
            for piece in split_text_recursive(body, chunk_size, overlap):
                emit(piece, types, pages)
        else:
            emit(body, types, pages)
        buffer = []
        buffer_len = 0

    for raw in blocks:
        block = raw if isinstance(raw, dict) else raw.to_dict()
        btype = block.get("type", "paragraph")
        text = (block.get("text") or "").strip()
        if not text:
            continue

        # 表格额外拼接语义化句子（检索用），但展示仍以 Markdown 为主
        semantic = (block.get("meta") or {}).get("semantic") or []
        retrieval_text = text + ("\n" + "\n".join(semantic) if semantic else "")

        if btype == "title":
            flush()  # 标题是天然边界：先落块，再更新章节栈
            level = max(int(block.get("level") or 1), 1)
            section_stack[:] = section_stack[: level - 1]
            while len(section_stack) < level - 1:
                section_stack.append("")
            section_stack.append(text)
            continue

        item = {"type": btype, "text": retrieval_text, "page": block.get("page")}

        # 表格独占一块，避免与其他内容混在一起导致语义混杂
        if btype == "table":
            flush()
            for piece in split_text_recursive(retrieval_text, chunk_size, overlap) or [retrieval_text]:
                emit(piece, ["table"], [block.get("page")] if block.get("page") else [])
            continue

        if buffer_len + len(retrieval_text) + 1 <= chunk_size:
            buffer.append(item)
            buffer_len += len(retrieval_text) + 1
        else:
            flush()
            if len(retrieval_text) > chunk_size:
                for piece in split_text_recursive(retrieval_text, chunk_size, overlap):
                    emit(piece, [btype], [block.get("page")] if block.get("page") else [])
            else:
                buffer.append(item)
                buffer_len = len(retrieval_text) + 1

    flush()
    return chunks


__all__ = ["split_text", "split_text_fixed", "split_text_recursive", "split_blocks"]