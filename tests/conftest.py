"""Shared test fixtures for Apple support routing and agent behavior."""

from __future__ import annotations

import json
from typing import Any

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage

from memory.long_term import LongTermMemory
from memory.working_memory import WorkingMemory


def _collect_text(messages: list[BaseMessage]) -> tuple[str, str]:
    system, human = "", ""
    for message in messages:
        if isinstance(message, SystemMessage):
            system += message.content
        elif isinstance(message, HumanMessage):
            human += message.content
    return system, human


_APPLE_DEFAULT_INTENT: dict[str, Any] = {
    "primary_intent": "query",
    "secondary_intent": "order_query",
    "confidence": 0.95,
    "entities": {"order_id": "ORD-001"},
    "candidates": [],
    "reason_code": "explicit_status_query",
    "suggested_agent": "ticket_handler",
}


class MockLLM:
    """Prompt-aware deterministic test double with Apple-domain defaults."""

    def __init__(self, overrides: dict[str, Any] | None = None):
        self.overrides = overrides or {}
        self.call_count = 0
        self.call_log: list[tuple[str, str]] = []

    async def ainvoke(self, messages: list[BaseMessage]) -> AIMessage:
        self.call_count += 1
        system, human = _collect_text(messages)
        self.call_log.append((system[:80], human[:80]))

        if "意图识别 Agent" in system or ("Apple" in system and "二级意图完整列表" in system):
            payload = dict(self.overrides.get("intent_router", _APPLE_DEFAULT_INTENT))
            payload.setdefault("candidates", [])
            payload.setdefault("reason_code", "ambiguous_request")
            payload.setdefault("suggested_agent", "knowledge_rag")
            return AIMessage(content=json.dumps(payload, ensure_ascii=False))

        if "工单处理 Agent" in system:
            payload = self.overrides.get(
                "ticket_handler",
                {
                    "action": "query",
                    "ticket_type": "general",
                    "priority": "medium",
                    "summary": "Apple 订单查询",
                    "details": human,
                    "ticket_id": "TK-TEST-001",
                },
            )
            return AIMessage(content=json.dumps(payload, ensure_ascii=False))

        if "相关性排序专家" in system:
            return AIMessage(content="0")
        if "知识库问答 Agent" in system:
            return AIMessage(content=self.overrides.get("rag_answer", "这是 Apple 支持知识库回答。"))
        if "安全与隐私审查" in system:
            payload = self.overrides.get("compliance", {"passed": True, "risk_level": "low", "violations": [], "suggestions": []})
            return AIMessage(content=json.dumps(payload, ensure_ascii=False))
        if "改写为更适合向量检索" in human:
            return AIMessage(content=self.overrides.get("rag_rewrite", "Apple 订单物流查询"))
        return AIMessage(content=self.overrides.get("default", "ok"))


class SequenceLLM:
    """Returns predefined outputs in sequence for format-repair and fallback tests."""

    def __init__(self, responses: list[str]):
        self.responses = responses
        self.call_count = 0
        self.call_log: list[tuple[str, str]] = []

    async def ainvoke(self, messages: list[BaseMessage]) -> AIMessage:
        index = min(self.call_count, len(self.responses) - 1)
        self.call_count += 1
        system, human = _collect_text(messages)
        self.call_log.append((system[:80], human[:80]))
        return AIMessage(content=self.responses[index])


@pytest.fixture
def working_memory() -> WorkingMemory:
    return WorkingMemory()


@pytest.fixture
def mock_llm() -> MockLLM:
    return MockLLM()


@pytest.fixture
def seeded_long_term_memory() -> LongTermMemory:
    memory = LongTermMemory()
    memory.add_document(
        "iPhone 和 AirPods 的使用与维修支持信息。维修服务是否可用取决于设备状态和适用政策。",
        "apple_product_support.md",
    )
    memory.add_document(
        "Apple 退款和订阅取消政策应以官方支持页面中的适用条款为准。",
        "apple_refund_subscription_policy.md",
    )
    return memory


@pytest.fixture
def base_state() -> dict[str, Any]:
    return {
        "messages": [HumanMessage(content="我的 Apple 订单什么时候到？")],
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
