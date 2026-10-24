"""失败导出（C 阶段）的单元测试：分类、按题聚合、质量/探针/故障转换。"""
from scripts.export_failures import export


def _retrieval_point(hit5=True, hit3=True, top1=True, doc_hit=True, ranks=None):
    return {
        "hit5": hit5,
        "hit3": hit3,
        "top1": top1,
        "doc_hit": doc_hit,
        "ranks": ranks if ranks is not None else ([1] if top1 else [2]),
    }


DATASET = {
    "questions": [
        {"id": 1, "question": "问题一", "doc": "文档A.md", "evidence": ["证据"]},
        {"id": 2, "question": "问题二", "doc": "文档B.md", "evidence": ["证据"]},
        {"id": 3, "question": "问题三", "doc": "文档C.md", "evidence": ["证据"]},
        {"id": 4, "question": "问题四", "doc": "文档D.md", "evidence": ["证据"]},
    ]
}


def _details():
    return {
        "dataset_md5": "abc",
        "results": {
            "vector": {
                "1": _retrieval_point(top1=False, ranks=[2]),
                "2": _retrieval_point(hit5=False, hit3=False, top1=False, doc_hit=True, ranks=[]),
                "3": _retrieval_point(hit5=False, hit3=False, top1=False, doc_hit=False, ranks=[]),
                "4": _retrieval_point(),
            },
            "hybrid": {
                "1": _retrieval_point(top1=False, ranks=[3]),
                "2": _retrieval_point(),
            },
        },
    }


class TestRetrievalClassification:
    def test_categories_and_grouping(self):
        result = export(details=_details(), dataset=DATASET)
        by_q = {f["question_id"]: f for f in result["failures"] if f["source"] == "retrieval"}

        # 问题 1：两个策略都是 Top-1 损失 -> 聚合成 1 条，列出两个策略
        assert by_q[1]["category"] == "ranking_noise"
        assert "vector" in by_q[1]["actual"] and "hybrid" in by_q[1]["actual"]
        # 问题 2：文档召回但分块未进 Top-5
        assert by_q[2]["category"] == "chunk_recall_miss"
        # 问题 3：连文档都错
        assert by_q[3]["category"] == "doc_recall_miss"
        # 问题 4：全对，不应出现
        assert 4 not in by_q

        assert result["counts"] == {"ranking_noise": 1, "chunk_recall_miss": 1, "doc_recall_miss": 1}
        assert all(f["id"].startswith("F-") for f in result["failures"])

    def test_severity(self):
        result = export(details=_details(), dataset=DATASET)
        severity = {f["category"]: f["severity"] for f in result["failures"]}
        assert severity["doc_recall_miss"] == "high"
        assert severity["chunk_recall_miss"] == "high"
        assert severity["ranking_noise"] == "low"


class TestQualityAndProbes:
    def test_quality_failure_detected(self):
        quality = {"items": [{"id": 9, "question": "q", "doc": "d",
                              "judge": {"correctness": 3, "faithfulness": 5}}]}
        result = export(details=None, dataset=DATASET, quality=quality)
        assert result["counts"] == {"answer_quality_fail": 1}

    def test_probe_hallucination_detected(self):
        probes = {"items": [{"id": "P1", "question": "q",
                             "judge": {"correctness": 5, "faithfulness": 5, "hallucination": True}}]}
        result = export(details=None, dataset=DATASET, probes=probes)
        assert result["counts"] == {"refusal_fail": 1}


class TestIncidents:
    def test_incidents_converted_with_fixed_status(self):
        incidents = {"incidents": [{
            "id": "INC-01", "title": "流式无输出", "category": "runtime_reliability",
            "severity": "high", "symptom": "只有 [DONE]", "root_cause": "事件不兼容",
            "fix": "换 stream_mode", "evidence": "commit 294f254",
        }]}
        result = export(details=None, dataset=DATASET, incidents=incidents)
        item = result["failures"][0]
        assert item["category"] == "runtime_reliability"
        assert item["status"] == "fixed"
        assert item["evidence"] == "commit 294f254"
        assert result["total"] == 1


class TestMetadata:
    def test_sources_and_counts(self):
        result = export(details=_details(), dataset=DATASET)
        assert result["sources"]["retrieval_points"] == 6
        assert result["total"] == sum(result["counts"].values())
        assert result["dataset_md5"] == "abc"
