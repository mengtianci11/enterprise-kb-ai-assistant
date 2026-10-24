"""指标聚合的单元测试：不可答题不计入检索指标，list/dict 两种输入都支持。"""
from scripts.eval_rag import aggregate

QUESTIONS_LIST = [
    {"id": 1, "question": "Q1", "type": "single_hop"},
    {"id": 2, "question": "Q2", "type": "unanswerable"},
]

RAW = {
    "vector": {
        "1": {"hit3": True, "hit5": True, "mrr": 1.0, "top1": True, "doc_hit": True},
        "2": {"hit3": False, "hit5": False, "mrr": 0.0, "top1": False, "doc_hit": False},
    }
}


def _agg(questions):
    return aggregate(
        RAW, questions, {}, n_docs=1, chunks=10, elapsed=1.0,
        dataset_label="eval/demo.json", n_unanswerable=1, n_total=2,
    )


def test_unanswerable_excluded_from_metrics():
    summary = _agg(QUESTIONS_LIST)
    row = next(r for r in summary["rows"] if r["strategy"] == "vector")
    # 只统计 1 道可答题，且该题全对 → 100%
    assert row["hit@3"] == 100.0
    assert row["mrr@5"] == 100.0
    assert summary["n"] == 1
    assert summary["n_total"] == 2
    assert summary["n_unanswerable"] == 1


def test_accepts_dict_questions():
    qmap = {str(q["id"]): q for q in QUESTIONS_LIST}
    summary = _agg(qmap)
    row = next(r for r in summary["rows"] if r["strategy"] == "vector")
    assert row["hit@5"] == 100.0


def test_empty_strategy_defaults_to_zero():
    summary = aggregate({}, QUESTIONS_LIST, {}, n_docs=1, chunks=1, elapsed=0,
                        dataset_label="x", n_total=2)
    assert all(r["hit@3"] == 0.0 and r["avg_ms"] == 0 for r in summary["rows"])
