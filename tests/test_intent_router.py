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

    assert out["intent"] == "order_query"
    ir = out["sub_results"]["intent_router"]
    assert ir["primary"] == "consultation"
    assert ir["secondary"] == "order_query"
    assert ir["confidence"] == 0.95
    assert "order_id" not in ir["entities"]


@pytest.mark.asyncio
async def test_refund_how_to_question_routes_to_knowledge():
    """退款政策/步骤咨询不能因为包含“退款”就创建工单。"""
    agent = IntentRouterAgent(MockLLM())

    result = await agent.classify("从 Apple 购买的 App 或内容怎么申请退款？")

    assert result.primary_intent == IntentCategory.CONSULTATION
    assert result.secondary_intent == "refund_policy"
    assert result.suggested_agent == "knowledge_rag"


@pytest.mark.asyncio
async def test_explicit_refund_request_routes_to_refund_handler():
    """明确要求代办退款仍然进入工单流程。"""
    agent = IntentRouterAgent(MockLLM())

    result = await agent.classify("我要申请退款，请帮我提交申请")

    assert result.primary_intent == IntentCategory.TRANSACTION
    assert result.secondary_intent == "refund_request"
    assert result.suggested_agent == "refund_handler"


@pytest.mark.asyncio
async def test_order_follow_up_does_not_create_ticket():
    """订单跟进语句应进入查询分支，即使模型原始结果偏向工单。"""
    agent = IntentRouterAgent(MockLLM())

    result = await agent.classify("换一个订单看看")

    assert result.primary_intent == IntentCategory.CONSULTATION
    assert result.secondary_intent == "order_query"
    assert result.confidence >= 0.85
    assert result.suggested_agent == "order_query"
    assert "order_id" not in result.entities


@pytest.mark.asyncio
async def test_explicit_order_signal_overrides_ticket_model_result():
    """订单/物流查询不能因为模型把 suggested_agent 写成工单而创建工单。"""
    agent = IntentRouterAgent(MockLLM())

    result = await agent.classify("查询订单 ORD-20260801-0005 的物流")

    assert result.secondary_intent == "order_query"
    assert result.suggested_agent == "order_query"
    assert result.entities["order_id"] == "ORD-20260801-0005"


@pytest.mark.asyncio
async def test_order_context_recognizes_pronoun_follow_up():
    """上一轮是订单查询时，“这个订单发货了吗”应保留订单查询意图。"""
    agent = IntentRouterAgent(MockLLM())

    result = await agent.classify(
        "这个订单发货了吗？",
        last_intent="order_query",
        context_entities={"order_id": "ORD-20260801-0005"},
    )

    assert result.secondary_intent == "order_query"
    assert result.suggested_agent == "order_query"


@pytest.mark.asyncio
async def test_complaint_request_overrides_order_wording():
    agent = IntentRouterAgent(MockLLM())

    result = await agent.classify("订单有问题，我要投诉")

    assert result.primary_intent == IntentCategory.COMPLAINT
    assert result.secondary_intent == "complaint"
    assert result.suggested_agent == "ticket_handler"


@pytest.mark.asyncio
@pytest.mark.parametrize("message", ["你好，请问你是谁？", "谢谢你", "刚才你说的第二项能力是什么？"])
async def test_conversation_can_change_topic_after_order_query(message):
    agent = IntentRouterAgent(MockLLM(overrides={"intent_router": {
        "primary_intent": "conversation", "secondary_intent": "smalltalk",
        "confidence": 0.95, "entities": {}, "suggested_agent": "conversation",
    }}))

    result = await agent.classify(message, last_intent="order_query", context_entities={"order_id": "ORD-001"})

    assert result.primary_intent == IntentCategory.CONVERSATION
    assert result.suggested_agent == "conversation"


@pytest.mark.asyncio
@pytest.mark.parametrize("message, expected", [
    ("你好，帮我查订单 ORD-20260801-0002", "order_query"),
    ("谢谢，帮我提交退款申请", "refund_handler"),
    ("你好，退款政策是什么？", "knowledge_rag"),
    ("你好，我要投诉服务问题", "ticket_handler"),
    ("你好，我的账户被盗了", "compliance_checker"),
])
async def test_business_request_takes_priority_over_conversation(message, expected):
    agent = IntentRouterAgent(MockLLM(overrides={"intent_router": {
        "primary_intent": "conversation", "secondary_intent": "greeting",
        "confidence": 0.95, "entities": {}, "suggested_agent": "conversation",
    }}))

    result = await agent.classify(message)

    assert result.suggested_agent == expected
