"""IntentRouterAgent 单元测试。"""

from __future__ import annotations

import json

import pytest
from langchain_core.messages import HumanMessage

from agents.intent_router import IntentCategory, IntentRouterAgent
from tests.conftest import MockLLM


@pytest.mark.asyncio
async def test_classify_parses_llm_json():
    llm = MockLLM(
        overrides={
            "intent_router": {
                "primary_intent": "consultation",
                "secondary_intent": "product_inquiry",
                "confidence": 0.92,
                "entities": {"product": "理财产品A"},
                "suggested_agent": "knowledge_rag",
            }
        }
    )
    agent = IntentRouterAgent(llm)
    result = await agent.classify("理财产品A收益多少？")

    assert result.primary_intent == IntentCategory.CONSULTATION
    assert result.secondary_intent == "product_inquiry"
    assert result.confidence == 0.92
    assert result.entities["product"] == "理财产品A"
    assert result.suggested_agent == "knowledge_rag"


@pytest.mark.asyncio
async def test_classify_invalid_json_fallback():
    class BadLLM:
        async def ainvoke(self, messages):
            from langchain_core.messages import AIMessage
            return AIMessage(content="not-json")

    agent = IntentRouterAgent(BadLLM())
    result = await agent.classify("hello")

    assert result.primary_intent == IntentCategory.UNKNOWN
    assert result.suggested_agent == "knowledge_rag"
    assert result.confidence == 0.0


@pytest.mark.asyncio
async def test_process_writes_intent_and_sub_results():
    llm = MockLLM()
    agent = IntentRouterAgent(llm)
    state = {
        "messages": [HumanMessage(content="查订单")],
        "sub_results": {},
    }
    out = await agent.process(state)

    assert out["intent"] == "ticket_handler"
    ir = out["sub_results"]["intent_router"]
    assert ir["primary"] == "consultation"
    assert ir["secondary"] == "order_query"
    assert ir["confidence"] == 0.95
    assert ir["entities"]["order_id"] == "ORD-001"
