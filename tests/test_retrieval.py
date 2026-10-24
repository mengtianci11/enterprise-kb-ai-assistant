"""检索管线单元测试（离线：伪向量 + 关闭外部 rerank）。"""


def _seed_kb(processor, rag):
    """写入两份带语义的文档并构建双索引。"""
    processor.add_document("制度.md", "考勤制度：员工每天工作9点到18点。年假规定：入职满一年享有5天带薪年假。".encode("utf-8"))
    processor.add_document("产品.md", "公司产品：企业ERP系统，支持进销存、财务核算与报表分析。".encode("utf-8"))
    rag.build_indices()


class TestIndexBuild:
    def test_build_and_reset(self, processor, rag):
        _seed_kb(processor, rag)
        assert rag.index_ready is True
        assert len(rag.docs) == len(processor.chunks) >= 2

        rag.reset()
        assert rag.index_ready is False

    def test_empty_kb(self, rag):
        rag.build_indices()  # 不应抛异常
        assert rag.index_ready is False
        assert rag.vector_search("年假") == []
        assert rag.bm25_search("年假") == []
        assert rag.retrieve("年假") == []


class TestSingleRetrieval:
    def test_vector_search(self, processor, rag):
        _seed_kb(processor, rag)
        results = rag.vector_search("年假有几天", top_k=5)
        assert results
        assert all("content" in r and "metadata" in r for r in results)

    def test_bm25_search(self, processor, rag):
        _seed_kb(processor, rag)
        results = rag.bm25_search("考勤 时间", top_k=5)
        assert results
        assert all("content" in r for r in results)


class TestFusion:
    def test_rrf_merge_rank_aware(self):
        r1 = [{"content": "A", "score": 0.9}, {"content": "B", "score": 0.8}]
        r2 = [{"content": "B", "score": 0.7}, {"content": "C", "score": 0.6}]
        merged = rag_module_rrf(r1, r2)
        assert merged[0]["content"] == "B"
        assert {m["content"] for m in merged} == {"A", "B", "C"}

    def test_rrf_merge_empty(self):
        assert rag_module_rrf([]) == []

    def test_rerank_fallback_when_disabled(self, processor, rag):
        _seed_kb(processor, rag)
        docs = rag._hybrid_retrieve("年假", top_k_rrf=5)
        ranked = rag.rerank_docs("年假", docs, top_n=2)
        assert len(ranked) <= 2  # RERANK_ENABLED=False → 原序截断
        assert "rerank_score" not in ranked[0]

    def test_strategy_search(self, processor, rag):
        _seed_kb(processor, rag)
        for strategy in ("vector", "bm25", "hybrid"):
            results = rag.strategy_search(strategy, "ERP 报表", top_k=3)
            assert results, f"策略 {strategy} 应返回结果"


class TestDomainSynonyms:
    def test_expand_query_adds_synonyms(self):
        from app.retrieval import expand_query

        expanded = expand_query("公司应聘一般要面几轮？")
        assert "面试" in expanded and "轮次" in expanded
        assert "数据外发" in expand_query("要给客户发送机密文件，需要走什么流程？")

    def test_expand_query_keeps_original(self):
        from app.retrieval import expand_query

        assert expand_query("完全无关的问题") == "完全无关的问题"

    def test_bm25_recalls_paraphrase_via_synonyms(self, processor, rag):
        """口语「面几轮」应能召回书面语「两轮面试」。"""
        processor.add_document(
            "招聘.md",
            "简历筛选通过后安排两轮面试：第一轮技术/业务面试，第二轮部门负责人终面。".encode("utf-8"),
        )
        rag.build_indices()
        results = rag.bm25_search("公司应聘一般要面几轮？", top_k=5)
        assert any("两轮面试" in r["content"] for r in results)


def rag_module_rrf(*args, **kwargs):
    from app.retrieval import AdvancedRAG

    return AdvancedRAG.rrf_merge(list(args), **kwargs)