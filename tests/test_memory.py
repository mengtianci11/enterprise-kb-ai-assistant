"""会话记忆测试：工作记忆窗口、滚动摘要、会话隔离、重启持久化。"""
import pytest

from app import config
from app.db import DocumentStore
from app.memory import build_context_messages, should_summarize, update_rolling_summary


@pytest.fixture
def store(tmp_path):
    return DocumentStore(db_path=str(tmp_path / "mem.db"))


@pytest.fixture
def conv(store):
    return store.get_or_create_conversation(1, "alice", "default", "conv-1", title="测试会话")


def _add_turns(store, conv, turns: int) -> None:
    for i in range(turns):
        store.add_message(conv["id"], "user", f"问题 {i}")
        store.add_message(conv["id"], "assistant", f"回答 {i}")


class TestConversationStore:
    def test_create_is_idempotent(self, store):
        c1 = store.get_or_create_conversation(1, "alice", "default", "k1")
        c2 = store.get_or_create_conversation(1, "alice", "default", "k1")
        assert c1["id"] == c2["id"]

    def test_same_key_isolated_per_user(self, store):
        c1 = store.get_or_create_conversation(1, "alice", "default", "same-key")
        c2 = store.get_or_create_conversation(2, "bob", "default", "same-key")
        assert c1["id"] != c2["id"]

    def test_messages_and_count(self, store, conv):
        store.add_message(conv["id"], "user", "你好")
        store.add_message(conv["id"], "assistant", "你好，有什么可以帮你")
        assert store.count_messages(conv["id"]) == 2
        msgs = store.list_messages(conv["id"])
        assert [m["role"] for m in msgs] == ["user", "assistant"]

    def test_list_messages_limit_returns_latest_in_order(self, store, conv):
        _add_turns(store, conv, 10)  # 20 条消息
        msgs = store.list_messages(conv["id"], limit=4)
        assert len(msgs) == 4
        assert [m["role"] for m in msgs] == ["user", "assistant", "user", "assistant"]
        assert msgs[-1]["content"] == "回答 9"

    def test_rename_and_delete(self, store, conv):
        assert store.rename_conversation(conv["id"], "新标题") is True
        assert store.get_conversation(conv["id"])["title"] == "新标题"
        assert store.delete_conversation(conv["id"]) is True
        assert store.get_conversation(conv["id"]) is None

    def test_list_conversations_orders_by_update(self, store):
        c1 = store.get_or_create_conversation(1, "alice", "default", "a")
        c2 = store.get_or_create_conversation(1, "alice", "default", "b")
        store.add_message(c1["id"], "user", "更新 c1")
        rows = store.list_conversations(1)
        assert rows[0]["id"] == c1["id"]
        assert {r["id"] for r in rows} == {c1["id"], c2["id"]}

    def test_persistence_across_store_reopen(self, tmp_path, store, conv):
        """模拟服务重启：新开一个 store 实例仍能读到会话与消息。"""
        store.add_message(conv["id"], "user", "重启前的问题")
        store.add_message(conv["id"], "assistant", "重启前的回答")

        reopened = DocumentStore(db_path=store.db_path)
        restored = reopened.get_or_create_conversation(1, "alice", "default", "conv-1")
        assert restored["id"] == conv["id"]
        msgs = reopened.list_messages(conv["id"])
        assert [m["content"] for m in msgs] == ["重启前的问题", "重启前的回答"]

    def test_ttl_purge(self, store, conv):
        assert store.purge_old_conversations(0) == 1
        assert store.get_conversation(conv["id"]) is None


class TestContextAssembly:
    def test_window_keeps_recent_turns(self, store, conv, monkeypatch):
        monkeypatch.setattr(config.settings, "CONTEXT_WINDOW_TURNS", 2)
        _add_turns(store, conv, 10)
        messages = build_context_messages(store, store.get_conversation(conv["id"]), "新问题")
        # 2 轮 = 4 条历史 + 本次提问
        assert len(messages) == 5
        assert messages[-1] == ("user", "新问题")
        assert messages[0][1] == "问题 8"  # 最近 4 条从「问题 8」开始

    def test_summary_folded_into_question(self, store, conv):
        """摘要以「问题前缀」注入（不能用 system 消息，否则 Agent 会跳过工具调用）。"""
        store.update_summary(conv["id"], "用户关注年假与报销", 0)
        messages = build_context_messages(store, store.get_conversation(conv["id"]), "继续问")
        assert all(role != "system" for role, _ in messages)
        assert messages[-1][0] == "user"
        assert "年假与报销" in messages[-1][1]
        assert "继续问" in messages[-1][1]

    def test_empty_conversation(self, store, conv):
        messages = build_context_messages(store, conv, "第一个问题")
        assert messages == [("user", "第一个问题")]


class TestRollingSummary:
    def test_triggers_only_after_threshold(self, store, conv, monkeypatch):
        monkeypatch.setattr(config.settings, "SUMMARY_TRIGGER_MESSAGES", 6)
        _add_turns(store, conv, 2)  # 4 条消息
        assert should_summarize(store, conv) is False
        _add_turns(store, conv, 1)  # 6 条消息
        assert should_summarize(store, store.get_conversation(conv["id"])) is True

    def test_summary_compresses_older_messages(self, store, conv, monkeypatch):
        monkeypatch.setattr(config.settings, "SUMMARY_TRIGGER_MESSAGES", 6)
        monkeypatch.setattr(config.settings, "SUMMARY_KEEP_RECENT", 2)
        _add_turns(store, conv, 5)  # 10 条消息

        calls = []

        def fake_chat(messages, temperature=0.2):
            calls.append(messages)
            return "摘要：用户询问了年假与报销制度。"

        fresh = store.get_conversation(conv["id"])
        summary = update_rolling_summary(store, fresh, fake_chat)
        assert summary == "摘要：用户询问了年假与报销制度。"
        assert calls, "应调用 LLM 进行摘要"

        stored = store.get_conversation(conv["id"])
        assert stored["summary"] == summary
        # 已摘要到倒数第 2 条消息（保留最近 2 条未摘要）
        assert stored["summarized_upto"] == store.list_messages(conv["id"])[-3]["id"]

        # 再次触发：剩余未摘要消息不足 keep 数量 → 不再调用
        calls.clear()
        assert update_rolling_summary(store, store.get_conversation(conv["id"]), fake_chat) is None
        assert calls == []

    def test_summary_failure_is_swallowed(self, store, conv, monkeypatch):
        monkeypatch.setattr(config.settings, "SUMMARY_TRIGGER_MESSAGES", 4)
        monkeypatch.setattr(config.settings, "SUMMARY_KEEP_RECENT", 2)
        _add_turns(store, conv, 4)

        def broken_chat(messages, temperature=0.2):
            raise RuntimeError("LLM 不可用")

        assert update_rolling_summary(store, store.get_conversation(conv["id"]), broken_chat) is None
