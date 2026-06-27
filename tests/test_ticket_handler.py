"""TicketHandlerAgent 实体消费测试。"""

from __future__ import annotations

import pytest
from langchain_core.messages import HumanMessage

from agents.ticket_handler import TicketHandlerAgent, TicketStore
from tests.conftest import MockLLM


class TestExtractEntityId:
    def test_ticket_id(self):
        assert TicketHandlerAgent._extract_entity_id({"ticket_id": "TK-1"}) == "TK-1"

    def test_order_id(self):
        assert TicketHandlerAgent._extract_entity_id({"order_id": "ORD-99"}) == "ORD-99"

    def test_chinese_keys(self):
        assert TicketHandlerAgent._extract_entity_id({"工单号": "W001"}) == "W001"
        assert TicketHandlerAgent._extract_entity_id({"订单号": "D002"}) == "D002"

    def test_empty(self):
        assert TicketHandlerAgent._extract_entity_id({}) is None


@pytest.mark.asyncio
async def test_process_uses_intent_router_entity_for_query():
    store = TicketStore()
    store.create("general", "medium", "已有工单", "详情", "u1")
    existing = list(store._tickets.values())[0]
    ticket_id = existing["ticket_id"]

    llm = MockLLM(
        overrides={
            "intent_router": {
                "primary_intent": "transaction",
                "secondary_intent": "order_query",
                "confidence": 0.9,
                "entities": {"order_id": ticket_id},
                "suggested_agent": "ticket_handler",
            },
            "ticket_handler": {
                "action": "query",
                "ticket_type": "general",
                "priority": "medium",
                "summary": "查询",
                "details": "查工单",
            },
        }
    )
    agent = TicketHandlerAgent(llm, ticket_store=store)
    state = {
        "messages": [HumanMessage(content=f"查工单 {ticket_id}")],
        "user_id": "u1",
        "sub_results": {
            "intent_router": {
                "entities": {"order_id": ticket_id},
                "confidence": 0.9,
            }
        },
    }
    out = await agent.process(state)

    assert ticket_id in out["sub_results"]["ticket_handler"]
