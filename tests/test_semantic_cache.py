"""语义缓存测试：命中/未命中、权限隔离、索引版本失效、TTL、LRU。"""
import pytest

from app import config
from app.semantic_cache import SemanticCache


def _vec(*values) -> list[float]:
    return list(values)


class TestScope:
    def test_same_scope_for_same_doc_set(self):
        cache = SemanticCache()
        assert cache.scope_key({3, 1, 2}) == cache.scope_key({1, 2, 3})

    def test_different_doc_set_different_scope(self):
        cache = SemanticCache()
        assert cache.scope_key({1, 2}) != cache.scope_key({1, 2, 3})

    def test_admin_scope_is_all(self):
        cache = SemanticCache()
        assert cache.scope_key(None).startswith("all:")

    def test_version_changes_scope(self):
        cache = SemanticCache()
        before = cache.scope_key({1})
        cache.bump_version()
        assert cache.scope_key({1}) != before


class TestCacheHit:
    def test_hit_on_identical_query(self):
        cache = SemanticCache(min_similarity=0.97)
        scope = cache.scope_key({1})
        result = [{"content": "年假 5 天"}]
        cache.put(_vec(1, 0, 0), scope, result)

        hit = cache.get(_vec(1, 0, 0), scope)
        assert hit == result
        assert cache.hits == 1

    def test_semantic_hit_on_similar_query(self):
        cache = SemanticCache(min_similarity=0.95)
        scope = cache.scope_key({1})
        cache.put(_vec(1.0, 0.05, 0.0), scope, [{"content": "命中"}])
        # 轻微差异（相似度 > 0.95）也应命中
        assert cache.get(_vec(1.0, 0.0, 0.0), scope) is not None

    def test_miss_on_different_question(self):
        cache = SemanticCache(min_similarity=0.97)
        scope = cache.scope_key({1})
        cache.put(_vec(1, 0, 0), scope, [{"content": "x"}])
        assert cache.get(_vec(0, 1, 0), scope) is None
        assert cache.misses == 1

    def test_miss_when_scope_differs(self):
        """核心安全测试：不同权限范围不能互相命中缓存。"""
        cache = SemanticCache()
        other = cache.scope_key({2})
        cache.put(_vec(1, 0, 0), cache.scope_key({1}), [{"content": "机密"}])
        assert cache.get(_vec(1, 0, 0), other) is None

    def test_disabled_cache_always_miss(self):
        cache = SemanticCache(enabled=False)
        scope = cache.scope_key({1})
        cache.put(_vec(1, 0, 0), scope, [{"content": "x"}])
        assert cache.get(_vec(1, 0, 0), scope) is None

    def test_empty_result_not_cached(self):
        cache = SemanticCache()
        scope = cache.scope_key({1})
        cache.put(_vec(1, 0, 0), scope, [])
        assert cache.stats()["entries"] == 0


class TestInvalidation:
    def test_bump_version_clears_entries(self):
        cache = SemanticCache()
        scope = cache.scope_key({1})
        cache.put(_vec(1, 0, 0), scope, [{"content": "x"}])
        assert cache.stats()["entries"] == 1

        cache.bump_version()
        assert cache.stats()["entries"] == 0
        # 旧 scope（旧版本）也不应命中
        assert cache.get(_vec(1, 0, 0), scope) is None

    def test_clear(self):
        cache = SemanticCache()
        cache.put(_vec(1, 0, 0), cache.scope_key({1}), [{"content": "x"}])
        cache.clear()
        assert cache.stats()["entries"] == 0

    def test_ttl_expiry(self, monkeypatch):
        cache = SemanticCache(ttl_seconds=60)
        scope = cache.scope_key({1})
        cache.put(_vec(1, 0, 0), scope, [{"content": "x"}])

        # 模拟时间流逝：把条目创建时间改到很早
        for entry in cache._entries:
            entry.created -= 120
        assert cache.get(_vec(1, 0, 0), scope) is None


class TestCapacity:
    def test_lru_capacity(self):
        cache = SemanticCache(max_entries=2)
        scope = cache.scope_key({1})
        cache.put(_vec(1, 0, 0), scope, [{"content": "a"}])
        cache.put(_vec(0, 1, 0), scope, [{"content": "b"}])
        cache.put(_vec(0, 0, 1), scope, [{"content": "c"}])
        assert cache.stats()["entries"] == 2
        # 最旧的被淘汰
        assert cache.get(_vec(1, 0, 0), scope) is None

    def test_similar_put_replaces(self):
        cache = SemanticCache(min_similarity=0.95)
        scope = cache.scope_key({1})
        cache.put(_vec(1, 0, 0), scope, [{"content": "旧"}])
        cache.put(_vec(0.99, 0.01, 0), scope, [{"content": "新"}])
        assert cache.stats()["entries"] == 1
        assert cache.get(_vec(1, 0, 0), scope)[0]["content"] == "新"


class TestRetrievalIntegration:
    def test_retrieve_skips_pipeline_on_cache_hit(self, processor, rag, monkeypatch):
        """命中缓存后不应再调用查询改写（证明跳过了整条检索管线）。"""
        processor.add_document("制度.md", "年假规定：入职满一年享有5天带薪年假。".encode("utf-8"))
        rag.build_indices()
        from app import llm

        cache = SemanticCache(min_similarity=0.9)
        rag.cache = cache

        calls = {"rewrite": 0}
        original_rewrite = rag.query_rewrite

        def counting_rewrite(question):
            calls["rewrite"] += 1
            return original_rewrite(question)

        monkeypatch.setattr(rag, "query_rewrite", counting_rewrite)
        monkeypatch.setattr(config.settings, "RAG_FUSION", True)

        first = rag.retrieve("年假有几天", top_n=3)
        second = rag.retrieve("年假有几天", top_n=3)

        assert first and second
        assert calls["rewrite"] >= 1
        rewrite_calls_after_first = calls["rewrite"]
        # 第二次应完全命中缓存，不再触发改写
        assert calls["rewrite"] == rewrite_calls_after_first
        assert cache.stats()["hits"] == 1

    def test_retrieve_respects_permission_scope(self, processor, rag):
        """不同可见文档集合不能复用缓存结果。"""
        processor.add_document("a.md", "销售部制度：提成 5%。".encode("utf-8"))
        processor.add_document("b.md", "研发部机密：算法密钥 ABC。".encode("utf-8"))
        rag.build_indices()
        docs = {d["name"]: d["id"] for d in processor.documents}
        rag.cache = SemanticCache(min_similarity=0.9)

        rag.retrieve("密钥 算法", top_n=5, allowed_doc_ids={docs["a.md"]})
        # 同样的问法，但管理员视角（不限制）→ 不应命中上一轮的缓存
        admin_result = rag.retrieve("密钥 算法", top_n=5, allowed_doc_ids=None)
        assert any("ABC" in r["content"] for r in admin_result)
