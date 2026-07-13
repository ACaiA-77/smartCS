"""Knowledge retrieval agent for grounded Apple support answers."""

from __future__ import annotations

from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from memory.long_term import LongTermMemory
from tracing.otel_config import trace_agent_call


RAG_SYSTEM_PROMPT = """你是 Apple 售后知识库问答 Agent，只能依据给出的参考文档回答。

回答规则：
1. 仅陈述参考文档明确支持的内容；没有足够依据时，明确说明并建议联系 Apple 官方支持。
2. 不承诺退款、维修结果、处理时效或账户状态。
3. 回答简洁、专业，并在末尾列出实际使用的来源名称。
4. 不要求用户提供密码、验证码、完整序列号或其他敏感信息。
"""

QUERY_REWRITE_PROMPT = """请将用户的 Apple 售后问题改写为更适合向量检索的查询语句。
保留产品、设备、订阅、订单或政策等核心实体；只返回改写后的查询，不要解释。

用户原始问题: {query}
"""

_PRODUCT_CONTEXT_SECONDARIES = {
    "product_support",
    "repair_warranty_policy",
    "return_refund_policy",
    "subscription_policy",
    "account_guidance",
    "sales_policy",
}


class KnowledgeRAGAgent:
    """Query rewrite, retrieval, deterministic fallback rerank, and grounded generation."""

    def __init__(
        self,
        llm: ChatOpenAI,
        long_term_memory: LongTermMemory | None = None,
        min_score: float = -1.0,
        enable_query_rewrite: bool = True,
        enable_llm_rerank: bool = True,
    ):
        self.llm = llm
        self.long_term_memory = long_term_memory or LongTermMemory(min_score=min_score)
        self.min_score = min_score
        self.enable_query_rewrite = enable_query_rewrite
        self.enable_llm_rerank = enable_llm_rerank

    @trace_agent_call("rag_query_rewrite")
    async def rewrite_query(self, original_query: str) -> str:
        response = await self.llm.ainvoke([HumanMessage(content=QUERY_REWRITE_PROMPT.format(query=original_query))])
        return response.content.strip() or original_query

    @trace_agent_call("rag_retrieve")
    async def retrieve_documents(self, query: str, top_k: int = 5) -> list[dict[str, Any]]:
        documents = self.long_term_memory.search(query, top_k=top_k)
        return [document for document in documents if float(document.get("score", 0.0)) >= self.min_score]

    @staticmethod
    def _deduplicate_documents(documents: list[dict[str, Any]]) -> list[dict[str, Any]]:
        seen: set[tuple[str, str]] = set()
        deduplicated: list[dict[str, Any]] = []
        for document in documents:
            metadata = document.get("metadata", {}) or {}
            key = (str(metadata.get("doc_id") or document.get("source", "")), str(document.get("content", "")))
            if key not in seen:
                seen.add(key)
                deduplicated.append(document)
        return deduplicated

    @trace_agent_call("rag_rerank")
    async def rerank_documents(
        self, query: str, documents: list[dict[str, Any]], top_k: int = 3
    ) -> list[dict[str, Any]]:
        candidates = self._deduplicate_documents(documents)
        if not candidates:
            return []

        summaries = "\n".join(f"[{index}] {document.get('content', '')[:200]}" for index, document in enumerate(candidates))
        response = await self.llm.ainvoke(
            [
                SystemMessage(content="你是 Apple 支持文档相关性排序专家。"),
                HumanMessage(
                    content=(
                        f"用户查询: {query}\n\n候选文档:\n{summaries}\n\n"
                        f"只返回最相关的 {top_k} 个索引号，使用逗号分隔，例如: 0,2"
                    )
                ),
            ]
        )

        try:
            ordered_indices: list[int] = []
            for value in response.content.split(","):
                index = int(value.strip())
                if 0 <= index < len(candidates) and index not in ordered_indices:
                    ordered_indices.append(index)
            if not ordered_indices:
                raise ValueError("no valid rerank indices")
            reranked = [candidates[index] for index in ordered_indices[:top_k]]
            selected = {index for index in ordered_indices}
            fallback = sorted(
                (document for index, document in enumerate(candidates) if index not in selected),
                key=lambda document: float(document.get("score", 0.0)),
                reverse=True,
            )
            return (reranked + fallback)[:top_k]
        except (TypeError, ValueError):
            return sorted(candidates, key=lambda document: float(document.get("score", 0.0)), reverse=True)[:top_k]

    @trace_agent_call("rag_generate")
    async def generate_answer(self, query: str, context_docs: list[dict[str, Any]]) -> str:
        if not context_docs:
            return "抱歉，知识库中没有足够的 Apple 售后资料来确认这个问题。建议您联系 Apple 官方支持获取帮助。"

        context = "\n\n---\n\n".join(
            f"来源: {document.get('source', '未知')}\n内容: {document.get('content', '')}"
            for document in context_docs
        )
        response = await self.llm.ainvoke(
            [
                SystemMessage(content=RAG_SYSTEM_PROMPT),
                HumanMessage(content=f"用户问题: {query}\n\n检索到的参考文档:\n{context}"),
            ]
        )
        return response.content

    @trace_agent_call("knowledge_rag_process")
    async def process(self, state: dict[str, Any]) -> dict[str, Any]:
        messages = state.get("messages", [])
        if not messages:
            return state

        original_query = messages[-1].content
        intent_info = state.get("sub_results", {}).get("intent_router", {})
        secondary = str(intent_info.get("secondary", ""))
        entities = dict(intent_info.get("entities", {}) or {})
        accumulated = state.get("sub_results", {}).get("_wm_context", {}).get("accumulated_entities", {}) or {}
        for key, value in accumulated.items():
            entities.setdefault(key, value)

        context_parts = [original_query]
        if secondary in _PRODUCT_CONTEXT_SECONDARIES:
            context_parts.insert(0, f"[{secondary}]")
        for key in ("product", "device_model", "subscription", "region"):
            if entities.get(key):
                context_parts.insert(1, str(entities[key]))
        retrieval_query = " ".join(context_parts)
        rewritten_query = await self.rewrite_query(retrieval_query) if self.enable_query_rewrite else retrieval_query
        raw_documents = await self.retrieve_documents(rewritten_query, top_k=5)
        reranked_documents = (
            await self.rerank_documents(rewritten_query, raw_documents, top_k=3)
            if self.enable_llm_rerank
            else sorted(
                self._deduplicate_documents(raw_documents),
                key=lambda document: float(document.get("score", 0.0)),
                reverse=True,
            )[:3]
        )
        answer = await self.generate_answer(original_query, reranked_documents)

        return {
            **state,
            "sub_results": {
                **state.get("sub_results", {}),
                "knowledge_rag": answer,
            },
        }
