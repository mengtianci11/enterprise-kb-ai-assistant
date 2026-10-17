"""结构感知分块（split_blocks）单元测试。"""
import pytest

from app.chunking import split_blocks


def B(btype: str, text: str, level: int = 0, **meta) -> dict:
    return {"type": btype, "text": text, "level": level, "meta": meta}


class TestSectionPath:
    def test_header_injected(self):
        blocks = [
            B("title", "第三章 休假制度", level=1),
            B("paragraph", "员工入职满一年后，每年享有5天带薪年假。"),
        ]
        chunks = split_blocks(blocks, doc_name="员工手册.md", chunk_size=300, overlap=50)
        assert len(chunks) == 1
        assert chunks[0]["section"] == "第三章 休假制度"
        assert chunks[0]["text"].startswith("[员工手册.md > 第三章 休假制度]")
        assert "带薪年假" in chunks[0]["text"]

    def test_header_can_be_disabled(self):
        blocks = [
            B("title", "总则", level=1),
            B("paragraph", "本手册适用于全体员工。"),
        ]
        chunks = split_blocks(blocks, doc_name="手册.md", include_header=False)
        assert chunks[0]["text"] == "本手册适用于全体员工。"

    def test_nested_sections_track_path(self):
        blocks = [
            B("title", "第三章 休假制度", level=1),
            B("paragraph", "第一节内容。"),
            B("title", "3.1 年假", level=2),
            B("paragraph", "年假为5天。"),
            B("title", "第四章 报销", level=1),
            B("paragraph", "报销需审批。"),
        ]
        chunks = split_blocks(blocks, doc_name="手册.md", chunk_size=100, overlap=10)
        sections = [c["section"] for c in chunks]
        assert sections == ["第三章 休假制度", "第三章 休假制度 > 3.1 年假", "第四章 报销"]
        assert all(c["text"].startswith("[手册.md > ") for c in chunks)


class TestTables:
    def test_table_isolated_and_semantic_kept(self):
        blocks = [
            B("title", "差旅标准", level=1),
            B("paragraph", "以下为住宿标准。"),
            B("table", "| 城市 | 标准 |\n| --- | --- |\n| 一线 | 500 |", semantic=["差旅标准：城市 为 一线，标准 为 500。"]),
        ]
        chunks = split_blocks(blocks, doc_name="手册.md", chunk_size=300, overlap=50)
        table_chunks = [c for c in chunks if "table" in c["block_types"]]
        assert len(table_chunks) == 1
        # 表格块必须带上语义化句子，且不与其他段落混在一起
        assert "标准 为 500" in table_chunks[0]["text"]
        assert table_chunks[0]["block_types"] == ["table"]


class TestSizing:
    def test_long_paragraph_is_split(self):
        blocks = [B("paragraph", "长" * 900)]
        chunks = split_blocks(blocks, doc_name="a.md", chunk_size=300, overlap=50)
        assert len(chunks) >= 2
        assert all(c["section"] == "" for c in chunks)

    def test_paragraphs_merge_up_to_limit(self):
        blocks = [
            B("title", "章节", level=1),
            B("paragraph", "A" * 100),
            B("paragraph", "B" * 100),
            B("paragraph", "C" * 100),
        ]
        chunks = split_blocks(blocks, doc_name="a.md", chunk_size=250, overlap=50)
        assert len(chunks) == 2  # A+B 合并，C 溢出到下一块
        assert all(len(c["text"]) <= 250 + 40 for c in chunks)  # 允许前缀开销

    def test_empty_input(self):
        assert split_blocks([], doc_name="a.md") == []
        assert split_blocks([B("paragraph", "   ")], doc_name="a.md") == []

    def test_invalid_params(self):
        with pytest.raises(ValueError):
            split_blocks([], chunk_size=10, overlap=10)
