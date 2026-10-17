"""进阶 RAG 检索管线：多路召回（稠密向量 + 稀疏 BM25）→ RRF 融合 → 重排序。

这是本项目的核心竞争力模块，也是简历上的核心技术点：
1. 稠密检索：FAISS（余弦相似度），捕捉语义相关性；
2. 稀疏检索：BM25（jieba 中文分词），捕捉词汇精确匹配；
3. 查询改写（RAG-Fusion）：LLM 将问题改写为 2-3 个不同视角的查询，
   提高长尾/口语化问题的召回率；
4. RRF（Reciprocal Rank Fusion）：不依赖分数尺度，稳健融合多路结果；
5. Cross-Encoder Rerank：用 bge-reranker 对 Top-20 精排，输出 Top-5；
6. 全链路容错降级：任何一环失败都不至于让整体崩溃。
"""
import logging
from typing import Optional

import app.llm as llm
from app.config import settings

logger = logging.getLogger(__name__)


class AdvancedRAG:
    """混合检索 + 重排序管线。"""

    def __init__(self, processor) -> None:
        self.dp = processor
        self._faiss_index = None      # faiss 索引
        self._chunk_index: list = []  # faiss 位置 -> chunk dict
        self.bm25 = None              # BM25 索引
        self.docs: list[str] = []     # BM25 语料（与 faiss 对齐）
        self.cache = None             # 语义缓存（由 AppState 注入；None 表示不启用）

    # ==================================================================
    # 索引构建
    # ==================================================================
    def build_indices(self) -> None:
        """从 processor.chunks 构建 FAISS + BM25 双索引。"""
        if not self.dp.chunks:
            logger.warning("没有文档数据，跳过索引构建")
            return

        import faiss
        import jieba
        import numpy as np
        from rank_bm25 import BM25Okapi

        embs = np.array([c["embedding"] for c in self.dp.chunks], dtype="float32")
        faiss.normalize_L2(embs)

        index = faiss.IndexFlatIP(embs.shape[1])
        index.add(embs)
        self._faiss_index = index
        self._chunk_index = list(self.dp.chunks)

        self.docs = [c["text"] for c in self.dp.chunks]
        tokenized_corpus = [list(jieba.cut(d)) for d in self.docs]
        self.bm25 = BM25Okapi(tokenized_corpus)
        logger.info("索引构建完成：FAISS %d 条 + BM25 %d 条", len(self.docs), len(self.docs))

    def reset(self) -> None:
        self._faiss_index = None
        self._chunk_index = []
        self.bm25 = None
        self.docs = []

    @property
    def index_ready(self) -> bool:
        return self._faiss_index is not None and self.bm25 is not None

    # ==================================================================
    # 单路召回（含权限过滤）
    # ==================================================================
    def _visible_indices(self, allowed_doc_ids: Optional[set[int]]) -> Optional[set[int]]:
        """把「可见文档 ID 集合」映射为「可见分块下标集合」。

        None 表示不限制（管理员/内部任务）。
        """
        if allowed_doc_ids is None:
            return None
        if not allowed_doc_ids:
            return set()
        return {
            i
            for i, chunk in enumerate(self._chunk_index)
            if chunk.get("doc_id") in allowed_doc_ids
        }

    def _is_visible(self, position: int, visible: Optional[set[int]]) -> bool:
        return visible is None or position in visible

    def vector_search(
        self,
        query: str,
        top_k: int = 10,
        allowed_doc_ids: Optional[set[int]] = None,
        query_embedding: Optional[list[float]] = None,
    ) -> list[dict]:
        """稠密向量检索（余弦相似度）。

        权限处理：FAISS 不支持元数据过滤，因此采用「宽召回 + 白名单过滤」——
        先取 `recall_pool` 个候选，再按可见集合过滤后截取 top_k。
        这样保证**候选集里不会出现无权文档**，而不是生成答案后才补救。
        """
        if self._faiss_index is None:
            return []
        import faiss
        import numpy as np

        visible = self._visible_indices(allowed_doc_ids)
        if visible is not None and not visible:
            return []  # 无任何可见文档，直接短路（避免无谓的向量化调用）

        q = np.array(
            [query_embedding if query_embedding is not None else llm.embed_text(query)],
            dtype="float32",
        )
        faiss.normalize_L2(q)
        pool = max(top_k, settings.RETRIEVAL_RECALL_POOL) if visible is not None else top_k
        k = min(pool, len(self._chunk_index))
        scores, idxs = self._faiss_index.search(q, k)

        results = []
        for j, i in enumerate(idxs[0]):
            if i < 0 or not self._is_visible(int(i), visible):
                continue
            results.append(
                {
                    "content": self._chunk_index[i]["text"],
                    "score": float(scores[0][j]),
                    "metadata": self._chunk_index[i],
                }
            )
            if len(results) >= top_k:
                break
        return results

    def bm25_search(
        self, query: str, top_k: int = 10, allowed_doc_ids: Optional[set[int]] = None
    ) -> list[dict]:
        """BM25 稀疏检索（jieba 中文分词）。

        权限处理：BM25 会为**全量语料**打分，因此必须先屏蔽无权分块的分值再排序，
        否则无权文档会挤占 top_k 名额（既泄露又降低有权内容的召回）。
        """
        if self.bm25 is None:
            return []
        import jieba
        import numpy as np

        visible = self._visible_indices(allowed_doc_ids)
        if visible is not None and not visible:
            return []

        tokenized_query = list(jieba.cut(query))
        scores = self.bm25.get_scores(tokenized_query)

        if visible is not None:
            mask = np.full(len(scores), -np.inf)
            idx_list = list(visible)
            mask[idx_list] = scores[idx_list]
            scores = mask

        top_k_indices = scores.argsort()[::-1][:top_k]
        return [
            {
                "content": self.docs[idx],
                "score": float(scores[idx]),
                "metadata": self._chunk_index[idx],
            }
            for idx in top_k_indices
            if np.isfinite(scores[idx]) and scores[idx] > float("-inf")
        ]

    # ==================================================================
    # 融合与精排
    # ==================================================================
    @staticmethod
    def rrf_merge(results_list: list[list[dict]], k: int = 60, top_k: int = 20) -> list[dict]:
        """Reciprocal Rank Fusion：多路结果按排名倒数融合。"""
        doc_score_map: dict = {}
        doc_info_map: dict = {}

        for one_result_list in results_list:
            for rank, doc_dict in enumerate(one_result_list):
                rrf_score = 1.0 / (k + rank + 1)
                doc_key = doc_dict["content"]  # 以内容为去重键
                if doc_key in doc_score_map:
                    doc_score_map[doc_key] += rrf_score
                else:
                    doc_score_map[doc_key] = rrf_score
                    doc_info_map[doc_key] = doc_dict

        sorted_docs = sorted(doc_score_map.items(), key=lambda x: x[1], reverse=True)
        final_result = []
        for doc_key, total_score in sorted_docs[:top_k]:
            doc_info = doc_info_map[doc_key].copy()
            doc_info["score"] = total_score
            final_result.append(doc_info)
        return final_result

    def rerank_docs(self, query: str, documents: list[dict], top_n: int = 5) -> list[dict]:
        """Cross-Encoder 重排序；异常时降级为按原序截断。"""
        if not settings.RERANK_ENABLED or not documents:
            return documents[:top_n]
        try:
            ranked = llm.rerank(query, documents, top_n=top_n)
        except Exception as exc:  # noqa: BLE001
            logger.warning("重排序服务不可用，降级使用原顺序 Top-%d：%s", top_n, exc)
            return documents[:top_n]
        for doc in ranked:
            if "relevance_score" in doc:
                doc["rerank_score"] = doc.pop("relevance_score")
        return ranked

    def query_rewrite(self, question: str) -> list[str]:
        """LLM 查询改写：从多个角度扩展检索入口。"""
        prompt = (
            "你是一个搜索专家。把下面这个问题改写为2-3个不同角度的搜索查询。\n"
            "每个查询使用不同的关键词组合，从不同侧面覆盖问题。\n"
            "直接输出改写后的查询，每行一个，不要编号，不要其他内容。\n"
            f"问题：{question}"
        )
        try:
            text = llm.chat([{"role": "user", "content": prompt}], temperature=0.3)
            return [q.strip() for q in text.splitlines() if q.strip()]
        except Exception as exc:  # noqa: BLE001
            logger.warning("查询改写失败，退回原问题检索：%s", exc)
            return [question]

    # ==================================================================
    # 对外完整管线
    # ==================================================================
    def _hybrid_retrieve(
        self,
        query: str,
        top_k_rrf: int = 20,
        allowed_doc_ids: Optional[set[int]] = None,
        query_embedding: Optional[list[float]] = None,
    ) -> list[dict]:
        """单查询混合召回：向量 + BM25 → RRF（两条召回路径都带权限过滤）。"""
        vector_docs = self.vector_search(
            query, top_k=10, allowed_doc_ids=allowed_doc_ids, query_embedding=query_embedding
        )
        bm25_docs = self.bm25_search(query, top_k=10, allowed_doc_ids=allowed_doc_ids)
        return self.rrf_merge([vector_docs, bm25_docs], top_k=top_k_rrf)

    def retrieve(
        self, question: str, top_n: int = 5, allowed_doc_ids: Optional[set[int]] = None
    ) -> list[dict]:
        """完整检索流水线（查询改写 → 混合召回 → RRF → Rerank），带语义缓存。

        allowed_doc_ids: 当前用户可见的文档 ID 集合；None 表示不限制（管理员）。
        权限过滤在**每一路召回的候选生成阶段**生效，因此最终的 RRF/Rerank
        候选池中不会出现无权文档——这正是企业级 RAG 与普通实现的本质区别。

        缓存（v3.3）：
        - 键 = 问题向量（语义近似）+ 权限范围指纹 + 索引版本；
        - 命中则直接返回，跳过改写/召回/重排（延迟与成本的降幅最大在这里）；
        - 问题向量只计算一次，未命中时复用给原始查询的向量检索，避免重复调用 embedding。
        """
        if not self.index_ready:
            return []

        cache = getattr(self, "cache", None)
        scope = None
        question_embedding: Optional[list[float]] = None

        if cache is not None and getattr(cache, "enabled", False):
            scope = cache.scope_key(allowed_doc_ids)
            try:
                question_embedding = llm.embed_text(question)
            except Exception as exc:  # noqa: BLE001
                logger.warning("查询向量化失败，本次跳过缓存：%s", exc)
                cache = None
            if cache is not None:
                cached = cache.get(question_embedding, scope)
                if cached is not None:
                    return cached[:top_n]

        queries = [question]
        if settings.RAG_FUSION:
            rewritten = self.query_rewrite(question)
            queries = [question, *[q for q in rewritten if q and q != question]]

        all_results = []
        for idx, query in enumerate(queries):
            all_results.append(
                self._hybrid_retrieve(
                    query,
                    top_k_rrf=20,
                    allowed_doc_ids=allowed_doc_ids,
                    query_embedding=question_embedding if idx == 0 else None,
                )
            )

        merged = self.rrf_merge(all_results, top_k=20)
        final = self.rerank_docs(question, merged, top_n=top_n)

        if cache is not None and question_embedding is not None:
            cache.put(question_embedding, scope, final)
        return final

    # ==================================================================
    # 评测辅助：暴露各策略，供离线评测（scripts/eval_rag.py）对比
    # ==================================================================
    def strategy_search(
        self,
        strategy: str,
        question: str,
        top_k: int = 5,
        allowed_doc_ids: Optional[set[int]] = None,
    ) -> list[dict]:
        """按策略检索，用于离线评测对比。strategy ∈ {vector, bm25, hybrid, fusion, full}

        说明：hybrid/fusion 采用「宽召回（Top-20 候选池）+ 窄输出（Top-k）」，
        与生产管线保持一致。
        """
        if strategy == "vector":
            return self.vector_search(question, top_k=top_k, allowed_doc_ids=allowed_doc_ids)
        if strategy == "bm25":
            return self.bm25_search(question, top_k=top_k, allowed_doc_ids=allowed_doc_ids)
        if strategy == "hybrid":
            return self._hybrid_retrieve(
                question, top_k_rrf=20, allowed_doc_ids=allowed_doc_ids
            )[:top_k]
        if strategy == "fusion":
            merged_all = []
            for q in [question, *self.query_rewrite(question)]:
                merged_all.append(
                    self._hybrid_retrieve(q, top_k_rrf=20, allowed_doc_ids=allowed_doc_ids)
                )
            return self.rrf_merge(merged_all, top_k=20)[:top_k]
        if strategy == "full":
            return self.retrieve(question, top_n=top_k, allowed_doc_ids=allowed_doc_ids)
        raise ValueError(f"未知检索策略: {strategy}")


__all__ = ["AdvancedRAG"]