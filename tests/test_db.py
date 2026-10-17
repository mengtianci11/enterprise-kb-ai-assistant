"""SQLite 持久化层单元测试。"""
import hashlib


def _make_chunks(n=3, dim=8):
    return [(f"chunk-{i}-内容-" * 10, [float(i + 1)] * dim) for i in range(n)]


class TestDocumentStore:
    def test_add_and_load_roundtrip(self, doc_store):
        chunks = _make_chunks()
        doc_id = doc_store.add_document("手册.md", hashlib.md5(b"x").hexdigest(), chunks)
        assert doc_id > 0
        docs, loaded = doc_store.load_all()
        assert len(docs) == 1 and docs[0]["name"] == "手册.md"
        assert docs[0]["chunks"] == 3
        assert len(loaded) == 3
        assert loaded[0]["source"] == "手册.md"
        assert loaded[0]["text"].startswith("chunk-0")
        assert len(loaded[0]["embedding"]) == 8

    def test_dedupe_by_content(self, doc_store):
        md5 = hashlib.md5(b"same-content").hexdigest()
        doc_store.add_document("a.txt", md5, _make_chunks(2))
        assert doc_store.doc_exists(md5) is True
        assert doc_store.doc_exists(hashlib.md5(b"other").hexdigest()) is False
        # 同名不同内容也判重
        assert doc_store.doc_exists(hashlib.md5(b"other").hexdigest(), name="a.txt") is True

    def test_delete_cascades(self, doc_store):
        doc_store.add_document("a.txt", hashlib.md5(b"a").hexdigest(), _make_chunks(3))
        doc_store.add_document("b.txt", hashlib.md5(b"b").hexdigest(), _make_chunks(2))
        assert doc_store.delete_document("a.txt") is True
        assert doc_store.delete_document("not-exist") is False
        docs, chunks = doc_store.load_all()
        assert len(docs) == 1 and docs[0]["name"] == "b.txt"
        assert len(chunks) == 2

    def test_stats_and_clear(self, doc_store):
        doc_store.add_document("a.txt", hashlib.md5(b"a").hexdigest(), _make_chunks(2))
        assert doc_store.stats() == {"documents": 1, "chunks": 2, "users": 0}
        doc_store.clear_all()
        assert doc_store.stats() == {"documents": 0, "chunks": 0, "users": 0}