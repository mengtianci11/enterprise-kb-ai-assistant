"""冲突与时效（B）的离线单元测试：候选筛选、检测、归因、显式说明。"""
from app.citation import Evidence
from app.conflicts import (
    Conflict,
    conflict_note,
    detect_conflicts,
    is_public,
    overlap_ratio,
    resolve_conflict,
    source_version,
)

V2025 = Evidence("E1", "员工入职满一年后，每年享有5天带薪年假。", "员工手册_v2025.md")
V2026 = Evidence("E2", "员工入职满一年后，每年享有7天带薪年假。", "员工手册_v2026.md")
LAW = Evidence("E3", "职工累计工作已满1年不满10年的，年休假5天。", "劳动法摘要_公开.md")


def _judge_conflict(a, b):
    return {"conflict": True, "fact": "年假天数", "value_a": "5 天", "value_b": "7 天"}


def _judge_no_conflict(a, b):
    return {"conflict": False}


class TestOverlap:
    def test_similar_high(self):
        assert overlap_ratio(V2025.text, V2026.text) > 0.5

    def test_unrelated_low(self):
        assert overlap_ratio(V2025.text, "完全无关的内容，讨论天气与旅游。") < 0.2


class TestSourceMeta:
    def test_public_detection(self):
        assert is_public("劳动法摘要_公开.md") is True
        assert is_public("员工手册_v2026.md") is False

    def test_version(self):
        assert source_version("员工手册_v2025.md") == 2025
        assert source_version("无版本文件.md") == 0


class TestDetect:
    def test_detects_conflict_pair(self):
        conflicts = detect_conflicts([V2025, V2026], judge=_judge_conflict)
        assert len(conflicts) == 1
        assert conflicts[0].fact == "年假天数"
        assert {conflicts[0].a.source, conflicts[0].b.source} == {
            "员工手册_v2025.md", "员工手册_v2026.md"
        }

    def test_no_conflict(self):
        assert detect_conflicts([V2025, V2026], judge=_judge_no_conflict) == []

    def test_unrelated_pairs_skipped(self):
        far = Evidence("E9", "今天天气很好，适合出门散步。", "无关.md")
        assert detect_conflicts([V2025, far], judge=_judge_conflict) == []


class TestResolve:
    def test_policy_question_prefers_newer_private(self):
        conflict = Conflict(V2025, V2026, "年假天数", "5 天", "7 天")
        resolved = resolve_conflict(conflict, "公司年假有几天？")
        assert resolved["winner"].source == "员工手册_v2026.md"
        assert "较新版本" in resolved["reason"]

    def test_law_question_prefers_public(self):
        conflict = Conflict(V2025, LAW, "年假天数", "5 天", "5 天")
        resolved = resolve_conflict(conflict, "按国家规定年假有几天？")
        assert resolved["winner"].source == "劳动法摘要_公开.md"
        assert "法定" in resolved["reason"]

    def test_old_version_question_prefers_older(self):
        conflict = Conflict(V2025, V2026, "年假天数", "5 天", "7 天")
        resolved = resolve_conflict(conflict, "旧版员工手册年假怎么规定？")
        assert resolved["winner"].source == "员工手册_v2025.md"
        assert "较早版本" in resolved["reason"]


class TestNote:
    def test_note_mentions_both_sources_and_winner(self):
        note = conflict_note([Conflict(V2025, V2026, "年假天数", "5 天", "7 天")], "年假有几天？")
        assert "员工手册_v2025.md" in note and "员工手册_v2026.md" in note
        assert "以 员工手册_v2026.md 为准" in note

    def test_empty_conflicts(self):
        assert conflict_note([], "x") == ""
