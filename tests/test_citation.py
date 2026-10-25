"""引用校验（A 阶段）的离线单元测试：证据构建、逐句校验、指标、删句兜底。"""
from app.citation import (
    Evidence,
    build_evidence,
    cited_ids,
    format_evidence,
    sanitize_answer,
    split_sentences,
    strip_citations,
    verify_answer,
)


def _docs():
    return [
        {"content": "年假：入职满一年 5 天，满三年 10 天。", "metadata": {"source": "员工手册.md", "chunk_id": "c1"}},
        {"content": "法定节假日加班按 3 倍工资。", "metadata": {"source": "员工手册.md", "chunk_id": "c2"}},
        {"content": "年假：入职满一年 5 天，满三年 10 天。", "metadata": {"source": "员工手册.md", "chunk_id": "c1"}},
    ]


def _judge(sentence, evidence_text):
    return "年假" in sentence and "年假" in evidence_text


class TestEvidence:
    def test_build_dedup_and_ids(self):
        evidence = build_evidence(_docs())
        assert [e.eid for e in evidence] == ["E1", "E2"]
        assert evidence[0].source == "员工手册.md"
        assert evidence[0].chunk_id == "c1"

    def test_limit(self):
        docs = [{"content": f"x{i}", "metadata": {"chunk_id": f"c{i}"}} for i in range(10)]
        assert len(build_evidence(docs, limit=3)) == 3

    def test_format(self):
        text = format_evidence([Evidence("E1", "年假 5 天", "员工手册.md")])
        assert "[E1]" in text and "员工手册.md" in text and "年假 5 天" in text


class TestSentenceUtils:
    def test_split(self):
        assert split_sentences("年假 5 天。加班 3 倍！") == ["年假 5 天。", "加班 3 倍！"]

    def test_cited_ids(self):
        assert cited_ids("年假 5 天[E1]。另见[E2][E3]。") == [1, 2, 3]

    def test_strip(self):
        assert strip_citations("年假 5 天[E1]。") == "年假 5 天。"


class TestVerify:
    def test_metrics(self):
        verification = verify_answer(
            "年假 5 天[E1]。加班 3 倍[E2]。无引用句。",
            build_evidence(_docs()),
            judge=_judge,
        )
        # E1 支持、E2 不支持 → 引用精确率 0.5；两句有引用、一句被支持 → 召回 0.5
        assert verification["citation_precision"] == 0.5
        assert verification["citation_recall"] == 0.5
        assert verification["n_cited_sentences"] == 2
        assert verification["uncited_rate"] == 0.333
        assert verification["sentences"][1]["supported"] is False

    def test_unknown_citation_id(self):
        verification = verify_answer("年假 5 天[E9]。", build_evidence(_docs()), judge=_judge)
        assert verification["n_citation_pairs"] == 0
        assert verification["citation_precision"] is None

    def test_no_citations(self):
        verification = verify_answer("直接回答。", build_evidence(_docs()), judge=_judge)
        assert verification["citation_recall"] is None
        assert verification["uncited_rate"] == 1.0


class TestSanitize:
    def test_drop_unsupported_sentence(self):
        verification = verify_answer(
            "年假 5 天[E1]。加班 3 倍[E2]。", build_evidence(_docs()), judge=_judge
        )
        answer, removed = sanitize_answer("年假 5 天[E1]。加班 3 倍[E2]。", verification)
        assert "加班" not in answer
        assert "年假" in answer
        assert removed == 1

    def test_all_removed_falls_back(self):
        verification = verify_answer("加班 3 倍[E2]。", build_evidence(_docs()), judge=_judge)
        answer, removed = sanitize_answer("加班 3 倍[E2]。", verification)
        assert "无法确认" in answer
        assert removed == 1
