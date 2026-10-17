"""质量门禁的纯逻辑测试（不调用外部 API）。"""
import json

import pytest

from scripts.check_metrics import DEFAULT_THRESHOLDS, compare


def _metrics(**overrides) -> dict:
    base = {
        "hit@3": 100.0,
        "hit@5": 100.0,
        "mrr@5": 97.9,
        "top1": 95.8,
        "doc_hit@5": 100.0,
    }
    base.update(overrides)
    return base


def _run(metrics: dict, dataset_md5: str = "abc") -> dict:
    return {"dataset_md5": dataset_md5, "metrics": {"full": metrics}}


def _baseline(metrics: dict, dataset_md5: str = "abc") -> dict:
    return {
        "dataset_md5": dataset_md5,
        "thresholds_pp": DEFAULT_THRESHOLDS,
        "metrics": {"full": metrics},
    }


class TestCompare:
    def test_no_regression_passes(self):
        assert compare(_run(_metrics()), _baseline(_metrics()), False) is True

    def test_improvement_passes(self):
        improved = _metrics(**{"mrr@5": 99.0})
        assert compare(_run(improved), _baseline(_metrics()), False) is True

    def test_small_drop_within_tolerance_passes(self):
        # MRR 阈值 3.0pp，下降 2pp 仍应通过
        dropped = _metrics(**{"mrr@5": 95.9})
        assert compare(_run(dropped), _baseline(_metrics()), False) is True

    def test_large_drop_fails(self):
        # MRR 下降 5pp > 3pp 阈值 → 失败
        dropped = _metrics(**{"mrr@5": 92.9})
        assert compare(_run(dropped), _baseline(_metrics()), False) is False

    def test_top1_regression_fails(self):
        dropped = _metrics(top1=88.0)  # 下降 7.8pp > 5pp
        assert compare(_run(dropped), _baseline(_metrics()), False) is False

    def test_dataset_change_fails_by_default(self):
        assert compare(_run(_metrics(), dataset_md5="changed"), _baseline(_metrics()), False) is False
        assert compare(_run(_metrics(), dataset_md5="changed"), _baseline(_metrics()), True) is True

    def test_missing_strategy_is_skipped(self):
        run = _run(_metrics())
        baseline = _baseline(_metrics())
        baseline["metrics"]["hybrid"] = _metrics(**{"mrr@5": 84.2})
        assert compare(run, baseline, False) is True
