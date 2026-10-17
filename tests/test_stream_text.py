"""流式文本提取的单元测试（跨 langchain 版本的稳健性）。"""
from langchain_core.messages import AIMessageChunk, HumanMessage, ToolMessage

from app.routes import _stream_text


class TestStreamText:
    def test_plain_ai_text(self):
        chunk = AIMessageChunk(content="年假 5 天")
        assert _stream_text(chunk) == "年假 5 天"

    def test_empty_content(self):
        assert _stream_text(AIMessageChunk(content="")) == ""

    def test_tool_call_chunk_skipped(self):
        chunk = AIMessageChunk(
            content="",
            tool_call_chunks=[{"name": "search_knowledge_base", "args": "{}", "index": 0}],
        )
        assert _stream_text(chunk) == ""

    def test_tool_message_skipped(self):
        tool_msg = ToolMessage(content="知识库检索到的参考内容如下：...", tool_call_id="1")
        assert _stream_text(tool_msg) == ""

    def test_human_message_skipped(self):
        assert _stream_text(HumanMessage(content="用户问题")) == ""

    def test_structured_content_list(self):
        chunk = AIMessageChunk(content=[{"type": "text", "text": "结构化"}, {"type": "other"}])
        assert _stream_text(chunk) == "结构化"

    def test_unknown_object_returns_empty(self):
        assert _stream_text(object()) == ""
