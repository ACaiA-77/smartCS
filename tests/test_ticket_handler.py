"""TicketHandlerAgent 实体消费测试。"""

from __future__ import annotations

import pytest
from langchain_core.messages import HumanMessage

from agents.ticket_handler import TicketHandlerAgent, TicketStore
from mcp.mcp_server import ToolCallResult
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


@pytest.mark.asyncio
async def test_complaint_does_not_reuse_stale_order_id_from_working_memory():
    store = TicketStore()
    llm = MockLLM(
        overrides={
            "ticket_handler": {
                "action": "create",
                "ticket_type": "complaint",
                "priority": "medium",
                "summary": "service complaint",
                "details": "user wants to complain about service",
            },
        }
    )
    agent = TicketHandlerAgent(llm, ticket_store=store)
    state = {
        "messages": [HumanMessage(content="我要投诉服务问题，请帮我创建工单")],
        "user_id": "u1",
        "sub_results": {
            "intent_router": {
                "primary": "complaint",
                "secondary": "complaint",
                "entities": {},
                "confidence": 0.9,
            },
            "_wm_context": {
                "last_intent": "ticket_handler",
                "accumulated_entities": {"order_id": "ORD-20260401-001"},
                "turn_count": 2,
            },
        },
    }

    out = await agent.process(state)

    assert "ORD-20260401-001" not in out["sub_results"]["ticket_handler"]
    assert len(store.query_by_user("u1")) == 1
    assert store.query_by_user("u1")[0]["type"] == "complaint"

class DemoOrderMcpServer:
    async def call_tool(self, name: str, arguments: dict) -> ToolCallResult:
        assert name == "order_query"
        assert arguments["order_id"] == "ORD-20260801-0001"
        return ToolCallResult(
            tool_name=name,
            success=True,
            result={
                "found": True,
                "data_source": "SQLite 本地国内电商演示数据",
                "order_id": "ORD-20260801-0001",
                "status": "in_transit",
                "status_label": "运输中",
                "payment_status_label": "已支付",
                "amount": 300.0,
                "product": "iPhone 16 256GB 黑色",
                "courier_company": "顺丰速运",
                "tracking_number": "SF202608010001",
                "after_sale_status_label": "无",
                "created_at": "2026-08-01T09:00:00",
            },
        )


@pytest.mark.asyncio
async def test_query_order_labels_public_demo_data_without_shipping_status():
    agent = TicketHandlerAgent(MockLLM(), mcp_server=DemoOrderMcpServer())

    response = await agent.query_order("ORD-20260801-0001", "user_001")

    assert "SQLite 本地国内电商演示数据" in response
    assert "运输中" in response
    assert "顺丰速运" in response
