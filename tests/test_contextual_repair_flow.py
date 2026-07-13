"""Regression coverage for contextual repair booking and clarification routing."""

from __future__ import annotations

import pytest
from langchain_core.messages import HumanMessage

from agents.intent_router import IntentRouterAgent
from agents.ticket_handler import TicketHandlerAgent
from tests.conftest import MockLLM, SequenceLLM


@pytest.mark.asyncio
async def test_contextual_repair_follow_up_routes_to_ticket_with_inherited_device():
    router = IntentRouterAgent(SequenceLLM(["not-json"]))
    state = {
        "messages": [HumanMessage(content="那我想预约维修")],
        "sub_results": {
            "_wm_context": {
                "last_intent": "knowledge_rag",
                "accumulated_entities": {"product": "iPhone 16"},
                "turn_count": 1,
            }
        },
    }

    result = await router.process(state)

    decision = result["sub_results"]["intent_router"]
    assert result["intent"] == "ticket_handler"
    assert result["needs_clarification"] is False
    assert decision["secondary"] == "repair_request"
    assert decision["reason_code"] == "context_follow_up"
    assert decision["entities"]["product"] == "iPhone 16"


@pytest.mark.asyncio
async def test_repair_ticket_collects_booking_fields_without_reasking_business_category():
    agent = TicketHandlerAgent(MockLLM())
    state = {
        "messages": [HumanMessage(content="那我想预约维修")],
        "user_id": "u-1",
        "sub_results": {
            "intent_router": {
                "secondary": "repair_request",
                "entities": {"product": "iPhone 16"},
            }
        },
    }

    result = await agent.process(state)

    response = result["sub_results"]["ticket_handler"]
    assert result["response_mode"] == "collect_ticket_details"
    assert "iPhone 16" in response
    assert "城市" in response or "地区" in response
    assert "查询 Apple 订单" not in response
    assert "验证码" in response

@pytest.mark.asyncio
async def test_graph_uses_ticket_flow_for_contextual_repair_not_compliance(working_memory):
    from agents.supervisor import create_supervisor_graph
    from memory.long_term import LongTermMemory

    session_id = "repair-follow-up"
    working_memory.update(
        session_id,
        {
            "last_intent": "knowledge_rag",
            "accumulated_entities": {"product": "iPhone 16"},
            "turn_count": 1,
        },
    )
    graph = create_supervisor_graph(
        llm=MockLLM(),
        working_memory=working_memory,
        long_term_memory=LongTermMemory(),
        enable_checkpointing=False,
    )
    result = await graph.ainvoke(
        {
            "messages": [HumanMessage(content="那我想预约维修")],
            "user_id": "u-1",
            "session_id": session_id,
            "intent": "",
            "sub_results": {},
            "compliance_passed": True,
            "final_response": "",
            "current_agent": "",
            "retry_count": 0,
            "needs_clarification": False,
            "response_mode": "execution",
        }
    )

    assert result["intent"] == "ticket_handler"
    assert result["sub_results"]["intent_router"]["secondary"] == "repair_request"
    assert result["response_mode"] == "collect_ticket_details"
    assert "iPhone 16" in result["final_response"]
    assert "查询 Apple 订单" not in result["final_response"]
