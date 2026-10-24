"""评测集校验器的单元测试（离线，使用临时语料目录）。"""
from pathlib import Path

from scripts.validate_dataset import validate


def _corpus(tmp_path: Path) -> Path:
    (tmp_path / "a.md").write_text("# A\n\n年假每年5天。\n", encoding="utf-8")
    return tmp_path


def _dataset(**overrides):
    q = {
        "id": 1,
        "question": "年假有几天？",
        "doc": "a.md",
        "evidence": ["年假每年5天"],
        "type": "single_hop",
    }
    q.update(overrides)
    return {"questions": [q]}


class TestValidation:
    def test_valid_passes(self, tmp_path):
        assert validate(_dataset(), check_chunks=False, corpus_dir=_corpus(tmp_path)) == []

    def test_valid_with_chunk_check(self, tmp_path):
        assert validate(_dataset(), check_chunks=True, corpus_dir=_corpus(tmp_path)) == []

    def test_missing_doc(self, tmp_path):
        errors = validate(_dataset(doc="不存在.md"), corpus_dir=_corpus(tmp_path))
        assert any("文档不存在" in e for e in errors)

    def test_evidence_not_substring(self, tmp_path):
        errors = validate(_dataset(evidence=["年假每年10天"]), corpus_dir=_corpus(tmp_path))
        assert any("原文子串" in e for e in errors)

    def test_missing_evidence(self, tmp_path):
        errors = validate(_dataset(evidence=[]), corpus_dir=_corpus(tmp_path))
        assert any("缺少 evidence" in e for e in errors)

    def test_unanswerable_skips_evidence(self, tmp_path):
        ds = {
            "questions": [
                {"id": 1, "question": "CEO 是谁？", "doc": "", "evidence": [], "type": "unanswerable"}
            ]
        }
        assert validate(ds, check_chunks=False, corpus_dir=_corpus(tmp_path)) == []

    def test_unanswerable_with_evidence_invalid(self, tmp_path):
        ds = {
            "questions": [
                {"id": 1, "question": "CEO 是谁？", "doc": "", "evidence": ["x"], "type": "unanswerable"}
            ]
        }
        errors = validate(ds, check_chunks=False, corpus_dir=_corpus(tmp_path))
        assert any("不应有 evidence" in e for e in errors)

    def test_duplicate_question(self, tmp_path):
        ds = {
            "questions": [
                {"id": 1, "question": "年假有几天？", "doc": "a.md", "evidence": ["年假每年5天"], "type": "single_hop"},
                {"id": 2, "question": "年假有几天？", "doc": "a.md", "evidence": ["年假每年5天"], "type": "single_hop"},
            ]
        }
        errors = validate(ds, check_chunks=False, corpus_dir=_corpus(tmp_path))
        assert any("问题重复" in e for e in errors)

    def test_duplicate_id(self, tmp_path):
        ds = {
            "questions": [
                {"id": 1, "question": "Q1", "doc": "a.md", "evidence": ["年假每年5天"], "type": "single_hop"},
                {"id": 1, "question": "Q2", "doc": "a.md", "evidence": ["年假每年5天"], "type": "single_hop"},
            ]
        }
        errors = validate(ds, check_chunks=False, corpus_dir=_corpus(tmp_path))
        assert any("重复 id" in e for e in errors)
