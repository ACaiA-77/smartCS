"""测试公共 fixture：Mock LLM 与初始 AgentState。"""

from __future__ import annotations

import json
from typing import Any

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage

from memory.working_memory import WorkingMemory
from memory.long_term import LongTermMemory


def _collect_text(messages: list[BaseMessage]) -> tuple[str, str]:
    system, human = "", ""
    for m in messages:
        if isinstance(m, SystemMessage):
            system += m.content
        elif isinstance(m, HumanMessage):
            human += m.content
    return system, human


class MockLLM:
    """按 Prompt 关键词返回预设 JSON/文本，避免真实 API 调用。"""

    def __init__(self, overrides: dict[str, Any] | None = None):
        self.overrides = overrides or {}
        self.call_count = 0
        self.call_log: list[tuple[str, str]] = []

    async def ainvoke(self, messages: list[BaseMessage]) -> AIMessage:
        self.call_count += 1
        system, human = _collect_text(messages)
        self.call_log.append((system[:80], human[:80]))

        if "意图识别Agent" in system:
            payload = self.overrides.get(
                "intent_router",
                {
                    "primary_intent": "consultation",
                    "secondary_intent": "order_query",
                    "confidence": 0.95,
                    "entities": {"order_id": "ORD-001"},
                    "suggested_agent": "ticket_handler",
                },
            )
            return AIMessage(content=json.dumps(payload, ensure_ascii=False))

        if "工单处理Agent" in system:
            payload = self.overrides.get(
                "ticket_handler",
                {
                    "action": "query",
                    "ticket_type": "general",
                    "priority": "medium",
                    "summary": "订单查询",
                    "details": human,
                    "ticket_id": "TK-TEST-001",
                },
            )
            return AIMessage(content=json.dumps(payload, ensure_ascii=False))

        if "文档相关性排序" in system or "文档相关性排序专家" in system:
            return AIMessage(content="0")

        if "知识库问答Agent" in system:
            return AIMessage(content=self.overrides.get("rag_answer", "这是知识库回答。"))

        if "合规审查Agent" in system or "金融合规审查" in system:
            payload = self.overrides.get(
                "compliance",
                {"passed": True, "risk_level": "low", "violations": [], "suggestions": []},
            )
            return AIMessage(content=json.dumps(payload, ensure_ascii=False))

        if "向量检索的查询语句" in human or "改写为更适合向量检索" in human:
            return AIMessage(content=self.overrides.get("rag_rewrite", "订单物流查询"))

        return AIMessage(content=self.overrides.get("default", "ok"))


@pytest.fixture
def working_memory() -> WorkingMemory:
    return WorkingMemory()


@pytest.fixture
def mock_llm() -> MockLLM:
    return MockLLM()


@pytest.fixture
def seeded_long_term_memory() -> LongTermMemory:
    """与 api/main lifespan 一致的默认知识库，供 RAG 集成测试使用。"""
    mem = LongTermMemory()
    mem.add_document(
        "我们的理财产品A年化收益率为3.5%-5.2%，投资期限为6个月至3年，最低投资金额10000元。"
        "注意：理财非存款，产品有风险，投资须谨慎。",
        "product_faq.md",
    )
    mem.add_document(
        "退款政策：用户在购买后7天内可申请无理由退款，超过7天需提供合理原因。"
        "退款将在3-5个工作日内原路退回。",
        "refund_policy.md",
    )
    return mem


@pytest.fixture
def base_state() -> dict[str, Any]:
    return {
        "messages": [HumanMessage(content="我的订单什么时候到？")],
        "user_id": "test-user",
        "session_id": "test-session",
        "intent": "",
        "sub_results": {},
        "compliance_passed": True,
        "final_response": "",
        "current_agent": "",
        "retry_count": 0,
        "needs_clarification": False,
    }
