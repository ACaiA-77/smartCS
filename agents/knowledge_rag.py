"""
知识检索Agent — RAG知识库问答
负责从向量数据库中检索相关文档，结合上下文生成准确回答。
实现完整的RAG流程：Query改写 → 向量检索 → 重排序 → 上下文注入 → 生成回答。
"""

from __future__ import annotations

import os
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from context.invocation import invoke_agent
from memory.long_term import LongTermMemory
from rag.models import RetrievalHit
from rag.runtime import RetrievalRuntime, RetrievalTrace
from tracing.otel_config import trace_agent_call


RAG_SYSTEM_PROMPT = """你是一个专业的知识库问答Agent，负责根据检索到的文档回答用户问题。

回答规则：
1. 严格基于检索到的文档内容回答，不要编造信息
2. 如果文档中没有相关信息，明确告知用户并建议转人工
3. 回答要简洁专业，适合客服场景
4. 对于金融产品信息，必须标注"以上信息仅供参考，具体以合同条款为准"
5. 在回答末尾标注引用的文档来源

回答格式：
- 先直接回答用户问题
- 如有必要补充相关信息
- 金融场景需添加风险提示
"""

QUERY_REWRITE_PROMPT = """请将用户的口语化问题改写为更适合向量检索的查询语句。
保留核心语义，去除口语化表达，补充专业术语。
只返回改写后的查询，不要其他内容。

用户原始问题: {query}
"""


class KnowledgeRAGAgent:
    """知识检索Agent - 实现完整RAG流程"""

    def __init__(
        self,
        llm: ChatOpenAI,
        long_term_memory: LongTermMemory | None = None,
        retriever=None,
        *,
        max_retrieval_rounds: int = 2,
        domains: list[str] | tuple[str, ...] | None = None,
    ):
        self.llm = llm
        self.long_term_memory = long_term_memory or LongTermMemory()
        self.retriever = retriever or self.long_term_memory.get_retriever(use_env=False)
        self.runtime = RetrievalRuntime(
            self.retriever, max_retrieval_rounds=max_retrieval_rounds
        )
        configured_domains = domains
        if configured_domains is None:
            value = os.getenv("RAG_DOMAINS", "")
            configured_domains = [item.strip() for item in value.split(",") if item.strip()]
        self.domains = tuple(configured_domains or ())
        self.enable_query_rewrite = self._env_flag("RAG_ENABLE_QUERY_REWRITE", default=True)
        self.enable_rerank = self._env_flag("RAG_ENABLE_RERANK", default=True)
        self._last_trace: RetrievalTrace | None = None

    @staticmethod
    def _env_flag(name: str, default: bool) -> bool:
        value = os.getenv(name)
        if value is None:
            return default
        return value.strip().lower() in {"1", "true", "yes", "on"}

    @trace_agent_call("rag_query_rewrite")
    async def rewrite_query(self, original_query: str) -> str:
        """Query改写：将口语化问题转为检索友好的查询"""
        messages = [
            HumanMessage(content=QUERY_REWRITE_PROMPT.format(query=original_query)),
        ]
        response = await invoke_agent(
            self.llm,
            "knowledge_rag.rewrite",
            messages,
            isolated=True,
        )
        return response.content.strip()

    @trace_agent_call("rag_retrieve")
    async def retrieve_documents(self, query: str, top_k: int = 5) -> list[dict]:
        """通过统一 HybridRetriever 检索相关文档。"""
        trace = self.runtime.retrieve(
            query,
            query,
            domains=self.domains,
            top_k=top_k,
            rerank=self.enable_rerank,
        )
        self._last_trace = trace
        return trace.hits

    @trace_agent_call("rag_rerank")
    async def rerank_documents(
        self, query: str, documents: list[dict], top_k: int = 3
    ) -> list[dict]:
        """兼容旧调用点，但排序统一交给注入的 Cross-Encoder。"""
        if not documents:
            return []
        candidates = [RetrievalHit.from_value(document) for document in documents]
        return self.retriever.rerank(query, candidates, top_k=top_k)

    async def retrieve_with_trace(
        self,
        original_query: str,
        rewritten_query: str,
        *,
        top_k: int = 3,
    ) -> RetrievalTrace:
        trace = self.runtime.retrieve(
            original_query,
            rewritten_query,
            domains=self.domains,
            top_k=top_k,
            rerank=self.enable_rerank,
        )
        self._last_trace = trace
        return trace

    @trace_agent_call("rag_generate")
    async def generate_answer(
        self,
        query: str,
        context_docs: list[dict],
        *,
        state: dict[str, Any] | None = None,
    ) -> str:
        """基于检索文档生成回答"""
        if not context_docs:
            return "抱歉，知识库中暂未找到与您问题相关的信息。建议您联系人工客服获取帮助。"

        messages = [
            SystemMessage(content=RAG_SYSTEM_PROMPT),
            HumanMessage(content=f"用户问题: {query}"),
        ]

        response = await invoke_agent(
            self.llm,
            "knowledge_rag",
            messages,
            state=state,
            task_message=f"用户问题: {query}",
            evidence=context_docs,
        )
        return response.content

    @trace_agent_call("knowledge_rag_process")
    async def process(self, state: dict[str, Any]) -> dict[str, Any]:
        """
        完整RAG流程（作为Graph节点）：
        1. 从 _session_context 读取累积实体补全 query
        2. Query改写
        3. 向量检索
        4. 重排序
        5. 生成回答
        """
        messages = state.get("messages", [])
        if not messages:
            return state

        original_query = messages[-1].content
        intent_info = state.get("sub_results", {}).get("intent_router", {})
        secondary = intent_info.get("secondary", "")
        entities = intent_info.get("entities", {}) or {}

        # 从工作记忆累积实体中补全（当本轮 intent_router 未提取到实体时）
        context = state.get("sub_results", {}).get("_session_context", {})
        accumulated = context.get("accumulated_entities", {}) or {}
        for key, val in accumulated.items():
            if key not in entities or not entities[key]:
                entities[key] = val

        rewrite_input = original_query
        if secondary == "product_inquiry" and entities.get("product"):
            rewrite_input = f"{entities['product']} {original_query}"
        elif secondary in ("product_inquiry", "policy_inquiry", "rate_inquiry"):
            rewrite_input = f"[{secondary}] {original_query}"

        rewritten_query = rewrite_input
        if self.enable_query_rewrite:
            rewritten_query = await self.rewrite_query(rewrite_input)

        trace = await self.retrieve_with_trace(
            original_query,
            rewritten_query,
            top_k=3,
        )
        reranked_docs = trace.hits

        answer = await self.generate_answer(original_query, reranked_docs, state=state)

        return {
            **state,
            "sub_results": {
                **state.get("sub_results", {}),
                "knowledge_rag": answer,
                "knowledge_rag_retrieval": trace.to_dict(),
            },
        }
