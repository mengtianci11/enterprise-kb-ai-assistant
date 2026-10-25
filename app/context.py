"""请求级上下文：把「当前用户可见的文档集合」沿调用链传递。

为什么需要（关键设计）：
RAG 的权限过滤必须在**召回阶段**生效，而不是生成答案之后。但 Agent 的检索工具
（`search_knowledge_base`）是在构建时闭包捕获 `rag` 的，无法直接拿到「当前请求的用户」。

方案：用 `contextvars` 承载请求级权限上下文——
- HTTP 路由在调用 Agent 前 `set()`，调用后 `reset()`；
- Agent 内部工具在检索时 `get()`，把可见文档集合传给检索管线；
- contextvars 天然异步/线程安全（每个请求独立副本），无需给工具传参或重建 Agent。

这样既保证了「不会出现越权召回」，又避免了「每个用户重建一次 Agent」的开销。
"""
from contextvars import ContextVar
from typing import Optional

# None = 不限制（管理员/系统内部调用）；空集合 = 无任何可见文档
current_allowed_docs: ContextVar[Optional[set[int]]] = ContextVar(
    "current_allowed_docs", default=None
)

# 当前用户名（用于审计与日志溯源）
current_username: ContextVar[str] = ContextVar("current_username", default="anonymous")

# 本轮请求中检索工具命中的证据（供引用校验使用；路由层每轮重置）
current_evidence: ContextVar[list] = ContextVar("current_evidence", default=[])


def set_evidence_context() -> object:
    """开始新一轮请求：清空证据列表，返回 token 供 reset。"""
    return current_evidence.set([])


def reset_evidence_context(token) -> None:
    current_evidence.reset(token)


def record_evidence(docs: list) -> None:
    """检索工具调用时记录证据（同一请求内多次检索会累积）。"""
    bucket = current_evidence.get()
    bucket.extend(docs or [])


def set_permission_context(allowed_docs: Optional[set[int]], username: str = "anonymous"):
    """设置权限上下文，返回 token 供调用方 reset。"""
    return (
        current_allowed_docs.set(allowed_docs),
        current_username.set(username),
    )


def reset_permission_context(tokens: tuple) -> None:
    doc_token, user_token = tokens
    current_allowed_docs.reset(doc_token)
    current_username.reset(user_token)


__all__ = [
    "current_allowed_docs",
    "current_username",
    "current_evidence",
    "set_permission_context",
    "reset_permission_context",
    "set_evidence_context",
    "reset_evidence_context",
    "record_evidence",
]
