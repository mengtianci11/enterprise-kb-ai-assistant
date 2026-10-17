"""文档处理服务：解析（委托解析子系统）→ 结构感知分块 → 向量化 → 持久化。

v3.0 变化：
- **解析解耦**：不再自己判断格式，统一调用 `app.parsers.parse_document()`，
  支持 9 类格式（含 OCR）；新增格式无需改动本文件（开闭原则）；
- **结构感知分块**：优先使用解析层的结构块（标题层级/表格）切分，
  块内注入章节路径；无结构信息时才回退到纯文本滑窗；
- **可观测的阶段回调**：`on_phase` 让上层（异步任务层）能实时更新任务状态；
- **原件留存**：原始文件按 MD5 落盘，便于重放解析与审计。
"""
import logging
from typing import Callable, Optional

import app.llm as llm
from app.chunking import split_blocks, split_text
from app.config import settings
from app.db import DocumentStore
from app.parsers import ParsedDocument, content_md5, needs_async, parse_document

logger = logging.getLogger(__name__)

PhaseCallback = Callable[[str], None]


class DocumentProcessor:
    """负责文档的解析、分块、向量化与持久化调度。"""

    def __init__(self, store: DocumentStore) -> None:
        self.store = store
        self.chunks: list[dict] = []      # [{"text","embedding","source","section","chunk_id"}]
        self.documents: list[dict] = []   # 文档清单（与持久化一致）
        self.embed_fn: Callable[[str], list[float]] = staticmethod(llm.embed_text)

    # ------------------------------------------------------------------
    # 解析（委托解析子系统）
    # ------------------------------------------------------------------
    def parse_file(
        self, filename: str, content: bytes, *, md5: Optional[str] = None, use_cache: bool = True
    ) -> tuple[ParsedDocument, bool]:
        """解析文档，返回 (解析结果, 是否命中缓存)。"""
        return parse_document(content, filename, md5=md5, use_cache=use_cache)

    def store_original(self, content: bytes, filename: str, md5: str) -> str:
        """留存原始文件（便于重放解析/审计）。"""
        from pathlib import Path

        suffix = Path(filename).suffix.lower()
        path = settings.UPLOAD_DIR / f"{md5}{suffix}"
        try:
            if not path.exists():
                path.write_bytes(content)
        except Exception as exc:  # noqa: BLE001
            logger.warning("原始文件留存失败：%s", exc)
        return str(path)

    # ------------------------------------------------------------------
    # 向量化
    # ------------------------------------------------------------------
    def get_embedding(self, text: str) -> list[float]:
        return self.embed_fn(text)

    # ------------------------------------------------------------------
    # 分块
    # ------------------------------------------------------------------
    def split_text(self, text: str) -> list[str]:
        return split_text(text, settings.CHUNK_STRATEGY, settings.CHUNK_SIZE, settings.CHUNK_OVERLAP)

    def build_chunks(self, parsed: ParsedDocument, doc_name: str) -> list[dict]:
        """结构感知分块；无结构信息时回退纯文本分块。"""
        if parsed.blocks:
            chunks = split_blocks(
                [b.to_dict() for b in parsed.blocks],
                doc_name=doc_name,
                chunk_size=settings.CHUNK_SIZE,
                overlap=settings.CHUNK_OVERLAP,
                include_header=settings.CHUNK_CONTEXT_HEADER,
            )
            if chunks:
                return chunks

        pieces = self.split_text(parsed.text)
        return [
            {"text": p, "section": "", "block_types": ["paragraph"], "pages": []} for p in pieces
        ]

    # ------------------------------------------------------------------
    # 增删改
    # ------------------------------------------------------------------
    def add_document(
        self,
        filename: str,
        content: bytes,
        *,
        md5: Optional[str] = None,
        use_cache: bool = True,
        on_phase: Optional[PhaseCallback] = None,
        owner_id: int = 0,
        classification: str = "internal",
        tenant_id: str = "",
        dept: str = "",
    ) -> dict:
        """完整流程：解析 → 分块 → 向量化 → 落库 → 刷新内存。

        owner_id / classification / tenant_id / dept 是权限体系所需的文档元数据。
        返回 {"chunks","warnings","format","pages","blocks","cached"}
        """
        md5 = md5 or content_md5(content)
        if self.store.doc_exists(md5, filename):
            raise ValueError(f"文档已存在（{filename} 或相同内容），请勿重复上传")

        if on_phase:
            on_phase("ocr" if needs_async(content, filename) else "parsing")

        parsed, cached = self.parse_file(filename, content, md5=md5, use_cache=use_cache)
        if parsed.is_empty:
            detail = "；".join(parsed.warnings) if parsed.warnings else "内容为空"
            raise ValueError(f"文档 {filename} 解析后无有效内容（{detail}）")

        chunk_dicts = self.build_chunks(parsed, filename)
        if not chunk_dicts:
            raise ValueError(f"文档 {filename} 未切分出有效分块")

        if on_phase:
            on_phase("embedding")

        items: list[dict] = []
        for chunk in chunk_dicts:
            items.append(
                {
                    "text": chunk["text"],
                    "embedding": self.get_embedding(chunk["text"]),
                    "section": chunk.get("section", ""),
                }
            )

        self.store_original(content, filename, md5)
        self.store.add_document(
            filename,
            md5,
            items,
            meta={
                "format": parsed.metadata.get("format", ""),
                "pages": parsed.metadata.get("pages", 0),
                "blocks": len(parsed.blocks),
                "status": "ready",
                "warnings": parsed.warnings,
                "parse_cached": cached,
                "owner_id": owner_id,
                "classification": classification,
                "tenant_id": tenant_id or settings.DEFAULT_TENANT,
                "dept": dept,
            },
        )
        self.rebuild_from_store()
        logger.info(
            "文档 %s 已入库：%d 个分块（格式=%s, 缓存命中=%s, 告警=%d）",
            filename,
            len(items),
            parsed.metadata.get("format", "?"),
            cached,
            len(parsed.warnings),
        )
        return {
            "chunks": len(items),
            "warnings": parsed.warnings,
            "format": parsed.metadata.get("format", ""),
            "pages": parsed.metadata.get("pages", 0),
            "blocks": len(parsed.blocks),
            "cached": cached,
        }

    def delete_document(self, filename: str) -> bool:
        ok = self.store.delete_document(filename)
        if ok:
            self.rebuild_from_store()
            logger.info("文档 %s 已删除", filename)
        return ok

    # ------------------------------------------------------------------
    # 状态管理
    # ------------------------------------------------------------------
    def rebuild_from_store(self) -> None:
        """从持久化存储重建内存态（启动恢复 / 增删后刷新共用）。"""
        self.documents, self.chunks = self.store.load_all()

    def clear_all(self) -> None:
        self.store.clear_all()
        self.rebuild_from_store()


__all__ = ["DocumentProcessor"]
