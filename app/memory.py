"""会话记忆层：三层记忆的实现（工作记忆 / 摘要记忆 / 会话管理）。

为什么不用 LangGraph 自带的 MemorySaver / SqliteSaver 做会话历史：
- 它们保存的是「整条线程状态」，**无法按 token 预算裁剪**，长会话成本只会越来越大；
- 本项目需要「最近 K 轮 + 摘要」这种可控的上下文组装方式（也是企业 RAG 的常规做法）。

设计：
1. **工作记忆**：每次请求只携带最近 `CONTEXT_WINDOW_TURNS` 轮原文（用户+助手各一条 = 1 轮），
   上下文 token 与轮数呈**常量关系**，而不是随会话线性增长；
2. **摘要记忆**：消息总数超过 `SUMMARY_TRIGGER_MESSAGES` 后触发滚动摘要——
   把「尚未摘要、且早于最近 SUMMARY_KEEP_RECENT 条」的旧消息交给 LLM 压缩成 ≤ SUMMARY_MAX_CHARS 的要点，
   增量合并进 `conversations.summary`（用 `summarized_upto` 记录已摘要的消息 id 上界，避免重复摘要）；
3. **会话管理**：会话与消息全部落 SQLite（按用户隔离），支持列表 / 重命名 / 删除 / TTL 清理，
   服务重启后历史与上下文完整保留。

代价说明（诚实记录）：不再依赖 LangGraph checkpointer 保存工具调用的中间状态，
每轮问答是「独立推理 + 历史上下文注入」；对知识库问答场景足够，
换来的是可控的 token 成本与可移植的会话数据。
"""
import logging
from typing import Optional

from app.config import settings
from app.db import DocumentStore

logger = logging.getLogger(__name__)


def build_context_messages(
    store: DocumentStore, conv: dict, user_message: str
) -> list[tuple[str, str]]:
    """组装本轮发给模型的上下文：摘要 + 最近 K 轮历史 + 本次提问。

    为什么摘要要并入用户消息、而不是作为 system 消息：
    实测发现给 create_agent 传 system 角色消息会让它跳过工具调用
    （直接回一句"请稍等，我为您查询"），因此摘要以「问题前缀」的形式携带，
    既保留上下文又不干扰 ReAct 的工具选择。
    """
    messages: list[tuple[str, str]] = []

    window = max(settings.CONTEXT_WINDOW_TURNS, 1) * 2
    history = store.list_messages(conv["id"], limit=window)
    for item in history:
        role = item["role"]
        if role in ("user", "assistant"):
            messages.append((role, item["content"]))

    summary = (conv.get("summary") or "").strip()
    if summary:
        user_message = (
            f"[此前对话摘要，供理解上下文]\n{summary}\n\n"
            f"[本次问题]\n{user_message}"
        )

    messages.append(("user", user_message))
    return messages


def should_summarize(store: DocumentStore, conv: dict) -> bool:
    return store.count_messages(conv["id"]) >= settings.SUMMARY_TRIGGER_MESSAGES


def update_rolling_summary(store: DocumentStore, conv: dict, chat_fn) -> Optional[str]:
    """滚动摘要：压缩「未摘要的旧消息」，增量合并进会话摘要。

    返回新摘要（未触发或失败时返回 None）。
    """
    try:
        total = store.count_messages(conv["id"])
        if total < settings.SUMMARY_TRIGGER_MESSAGES:
            return None

        upto = conv.get("summarized_upto") or 0
        keep = max(settings.SUMMARY_KEEP_RECENT, 2)

        # 需要摘要的候选：summarized_upto 之后、且排除最近 keep 条
        pending = store.list_messages(conv["id"], after_id=upto)
        if len(pending) <= keep:
            return None
        to_summarize = pending[:-keep]
        if not to_summarize:
            return None

        history_text = "\n".join(
            f"[{m['role']}] {m['content']}" for m in to_summarize
        )[: settings.SUMMARY_MAX_CHARS * 4]
        previous = (conv.get("summary") or "").strip()

        prompt = (
            "你是会话记忆压缩器。请把下面的对话压缩成要点摘要，保留：用户关注的主题、"
            "已确认的事实与结论、未解决的问题。输出不超过 300 字的纯文本，不要寒暄。\n\n"
            f"{('已有摘要：' + previous + chr(10) + chr(10)) if previous else ''}"
            f"需要压缩的对话：\n{history_text}"
        )
        summary = chat_fn(
            [{"role": "user", "content": prompt}], temperature=0.2
        ).strip()[: settings.SUMMARY_MAX_CHARS]

        if not summary:
            return None
        store.update_summary(conv["id"], summary, to_summarize[-1]["id"])
        logger.info(
            "会话 %s 摘要已更新：压缩 %d 条消息，摘要长度 %d 字",
            conv["id"], len(to_summarize), len(summary),
        )
        return summary
    except Exception as exc:  # noqa: BLE001
        logger.warning("会话摘要更新失败（不影响主流程）：%s", exc)
        return None


__all__ = ["build_context_messages", "should_summarize", "update_rolling_summary"]
