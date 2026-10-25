"""Agent 层：基于 LangGraph 的 ReAct 工具调用代理。

工具集：
- search_knowledge_base：检索企业知识库（RAG 主工具）；
- calculator：安全数学计算（限制内建函数，防注入）；
- transfer_to_agent：知识库无法覆盖时转人工客服。

升级点：改用 langchain.agents.create_agent（替代已弃用的
langgraph.prebuilt.create_react_agent），并保留降级兼容。
"""
import logging
from typing import Optional

import app.llm as llm  # noqa: F401  (保留引用，便于测试打桩)
from app.config import settings
from app.context import current_allowed_docs, current_evidence, record_evidence

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """你是企业知识库AI助手。
- 公司制度、产品、技术问题 → 用 search_knowledge_base 检索后回答
- 数学计算 → 用 calculator
- 知识库查不到或用户要投诉/找人工 → 用 transfer_to_agent
- 简单问候直接回答
回答要简洁专业；引用检索资料时必须在句末标注证据编号（如 [E1]），证据不足时明确说明。"""


def build_agent(rag, llm_model: Optional[object] = None, checkpointer: Optional[object] = None):
    """构建 LangGraph ReAct Agent。

    rag: AdvancedRAG 实例（工具闭包引用）。
    checkpointer: 可选。本项目默认不传——会话历史由 app/memory.py 自行管理
        （工作记忆窗口 + 滚动摘要），以便按 token 预算裁剪上下文；
        如需 LangGraph 原生线程级记忆，可传入 SqliteSaver 等实现。
    """
    from langchain_core.tools import tool
    from langchain_openai import ChatOpenAI

    llm_model = llm_model or ChatOpenAI(
        model=settings.CHAT_MODEL,
        api_key=settings.API_KEY,
        base_url=settings.BASE_URL,
        temperature=0.3,
    )

    @tool
    def search_knowledge_base(query: str) -> str:
        """检索企业知识库，查询公司制度、产品说明、技术文档等内部资料。当用户问到公司相关问题时使用。"""
        try:
            # 关键：检索时带上「当前用户可见文档集合」，
            # 让权限过滤发生在召回阶段，而不是生成答案之后
            # （详见 app/context.py 与 docs/design_auth.md）
            allowed = current_allowed_docs.get()
            docs = rag.retrieve(query, allowed_doc_ids=allowed)
            if not docs:
                return "知识库中未检索到与问题相关的内容。"
            # 记录本轮证据，供引用校验（app/citation.py）使用；
            # 编号按全局偏移递增，避免多次检索时 [E1] 冲突
            start = len(current_evidence.get())
            record_evidence(docs)
            result_text = "知识库检索到的参考内容如下：\n"
            for idx, doc in enumerate(docs, start=1):
                source = (doc.get("metadata") or {}).get("source", "未知来源")
                result_text += f"\n[E{start + idx}] (来源: {source})\n{doc['content']}\n"
            return result_text
        except Exception as exc:  # noqa: BLE001
            return f"知识库检索暂时不可用，错误原因：{str(exc)}"

    @tool
    def calculator(expression: str) -> str:
        """计算数学表达式，如 '3000 * 0.8'。"""
        try:
            result = eval(expression, {"__builtins__": {}}, {})  # noqa: S307
            return str(result)
        except ZeroDivisionError:
            return "错误：除数不能为零"
        except SyntaxError:
            return "错误：表达式语法有误，请检查运算符号与括号格式"
        except Exception as exc:  # noqa: BLE001
            return f"计算失败：{str(exc)}"

    @tool
    def transfer_to_agent(summary: str) -> str:
        """当问题无法通过知识库回答，或用户明确要求人工服务时，转接人工客服。输入问题摘要。"""
        return f"已为您转接人工客服。问题摘要：{summary}"

    tools = [search_knowledge_base, calculator, transfer_to_agent]

    # 优先使用 langchain.agents.create_agent（V1 正式 API）；
    # 仅当该 API 不存在（旧版本）时才回退 langgraph.prebuilt
    try:
        from langchain.agents import create_agent

        agent = create_agent(
            llm_model,
            tools,
            system_prompt=SYSTEM_PROMPT,
            checkpointer=checkpointer,
        )
        logger.info("Agent 构建完成（langchain.agents.create_agent）")
        return agent
    except (ImportError, TypeError, AttributeError) as exc:
        logger.warning("create_agent 不可用（%s），回退 langgraph.prebuilt", exc)
        from langgraph.prebuilt import create_react_agent

        agent = create_react_agent(
            llm_model,
            tools,
            prompt=SYSTEM_PROMPT,
            checkpointer=checkpointer,
        )
        logger.info("Agent 构建完成（langgraph.prebuilt.create_react_agent，兼容模式）")
        return agent


__all__ = ["build_agent", "SYSTEM_PROMPT"]