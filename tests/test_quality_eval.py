"""LLM-as-Judge 评测的纯逻辑测试（Judge 解析与指标聚合，不调用外部 API）。"""
from scripts.eval_quality import parse_judge_json, summarize


class TestParseJudgeJson:
    def test_plain_json(self):
        result = parse_judge_json(
            '{"correctness": 5, "faithfulness": 4, "relevance": 5, "hallucination": false, "reason": "准确"}'
        )
        assert result["correctness"] == 5
        assert result["faithfulness"] == 4
        assert result["hallucination"] is False

    def test_fenced_json(self):
        text = '评估如下：\n```json\n{"correctness": 4, "faithfulness": 5, "relevance": 4, "hallucination": false, "reason": "ok"}\n```\n完毕'
        result = parse_judge_json(text)
        assert result["correctness"] == 4 and result["relevance"] == 4

    def test_json_with_surrounding_text(self):
        result = parse_judge_json(
            '评分：{"correctness": 3, "faithfulness": 3, "relevance": 4, "hallucination": true, "reason": "部分编造"} 以上'
        )
        assert result["hallucination"] is True
        assert result["faithfulness"] == 3

    def test_clamps_out_of_range(self):
        result = parse_judge_json(
            '{"correctness": 9, "faithfulness": 0, "relevance": -2, "hallucination": false, "reason": "x"}'
        )
        assert result["correctness"] == 5
        assert result["faithfulness"] == 1  # 0/负数被夹到 1，不视为无效
        assert result["relevance"] == 1

    def test_missing_field_returns_none(self):
        assert parse_judge_json('{"correctness": 5}') is None

    def test_invalid_json_returns_none(self):
        assert parse_judge_json("不是 JSON") is None
        assert parse_judge_json("") is None


class TestSummarize:
    def _item(self, c=None, f=None, r=None, h=False):
        judge = None
        if c is not None:
            judge = {"correctness": c, "faithfulness": f, "relevance": r, "hallucination": h, "reason": ""}
        return {"judge": judge}

    def test_aggregates_judged_only(self):
        items = [
            self._item(5, 5, 5),
            self._item(4, 4, 4),
            self._item(2, 1, 3, h=True),
            self._item(),  # 评分失败，不应计入分母
        ]
        metrics = summarize(items)
        assert metrics["n"] == 4
        assert metrics["judged"] == 3
        assert metrics["correctness"] == round((5 + 4 + 2) / 3, 2)
        assert metrics["pass_rate"] == round(2 / 3 * 100, 1)
        assert metrics["hallucination_rate"] == round(1 / 3 * 100, 1)

    def test_all_failed_returns_zeros(self):
        metrics = summarize([{"judge": None}, {"judge": None}])
        assert metrics["judged"] == 0
        assert metrics["correctness"] == 0.0
        assert metrics["pass_rate"] == 0.0

    def test_empty(self):
        metrics = summarize([])
        assert metrics["n"] == 0 and metrics["judged"] == 0
