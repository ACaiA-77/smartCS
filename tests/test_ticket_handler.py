"""TicketHandlerAgent contract tests."""

from __future__ import annotations

import pytest
from langchain_core.messages import HumanMessage

from agents.ticket_handler import TicketHandlerAgent
from mcp.execution_ledger import ExecutionLedger
from mcp.mcp_server import MCPToolServer, ToolDefinition, create_default_tools
from mcp.order_repository import OrderRepository
from mcp.tool_execution import ToolExecutor
from tests.conftest import MockLLM
from tickets.service import TicketService


def _agent(tmp_path, llm=None):
    repository = OrderRepository(str(tmp_path / "orders.db"))
    server = create_default_tools(MCPToolServer(), order_repository=repository)
    executor = ToolExecutor(server, ledger=ExecutionLedger(repository.db_path))
    return TicketHandlerAgent(llm or MockLLM(), tool_executor=executor), repository


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
async def test_process_queries_durable_ticket_by_intent_entity(tmp_path):
    _, repository = _agent(tmp_path)
    service = TicketService(repository)
    created = service.create_ticket(
        client_request_id="client-query",
        user_id="u1",
        title="已有工单",
        description="详情",
    )
    ticket_id = created["ticket_id"]
    agent, _ = _agent(
        tmp_path,
        MockLLM(
            overrides={
                "ticket_handler": {
                    "action": "query",
                    "ticket_id": ticket_id,
                    "summary": "查询",
                    "details": "查工单",
                }
            }
        ),
    )
    state = {
        "messages": [HumanMessage(content=f"查工单 {ticket_id}")],
        "user_id": "u1",
        "sub_results": {
            "intent_router": {
                "entities": {"ticket_id": ticket_id},
                "secondary": "ticket_query",
                "confidence": 0.9,
            }
        },
    }

    out = await agent.process(state)

    assert ticket_id in out["sub_results"]["ticket_handler"]
    assert "工单查询结果" in out["sub_results"]["ticket_handler"]


@pytest.mark.asyncio
async def test_complaint_does_not_reuse_stale_order_id_from_session_state(tmp_path):
    agent, repository = _agent(
        tmp_path,
        MockLLM(
            overrides={
                "ticket_handler": {
                    "action": "create",
                    "ticket_type": "complaint",
                    "priority": "medium",
                    "summary": "service complaint",
                    "details": "user wants to complain about service",
                },
            }
        ),
    )
    state = {
        "messages": [HumanMessage(content="我要投诉服务问题，请帮我创建工单")],
        "user_id": "u1",
        "session_id": "session-1",
        "sub_results": {
            "intent_router": {
                "primary": "complaint",
                "secondary": "complaint",
                "entities": {},
                "confidence": 0.9,
            },
            "_session_context": {
                "last_intent": "ticket_handler",
                "accumulated_entities": {"order_id": "ORD-20260401-001"},
                "turn_count": 2,
            },
        },
    }

    out = await agent.process(state)

    assert "ORD-20260401-001" not in out["sub_results"]["ticket_handler"]
    with repository.transaction() as connection:
        assert connection.execute("SELECT COUNT(*) FROM support_tickets").fetchone()[0] == 1


class DemoOrderMcpServer:
    async def order_query(self, order_id: str, user_id: str) -> dict:
        assert order_id == "ORD-20260801-0001"
        return {
            "found": True,
            "data_source": "SQLite 本地国内电商演示数据",
            "order_id": order_id,
            "status": "in_transit",
            "status_label": "运输中",
            "payment_status_label": "已支付",
            "amount": 300.0,
            "product": "iPhone 16 256GB 黑色",
            "courier_company": "顺丰速运",
            "tracking_number": "SF202608010001",
            "after_sale_status_label": "无",
            "created_at": "2026-08-01T09:00:00",
        }


@pytest.mark.asyncio
async def test_query_order_labels_public_demo_data_without_shipping_status():
    server = MCPToolServer()
    server.register_tool(
        ToolDefinition(
            name="order_query",
            description="test",
            input_schema={"type": "object"},
            handler=DemoOrderMcpServer().order_query,
        )
    )
    agent = TicketHandlerAgent(MockLLM(), tool_executor=ToolExecutor(server))

    response = await agent.query_order("ORD-20260801-0001", "user_001")

    assert "SQLite 本地国内电商演示数据" in response
    assert "运输中" in response
    assert "顺丰速运" in response


@pytest.mark.asyncio
async def test_switch_order_does_not_reuse_previous_order_or_create_ticket(tmp_path):
    agent, repository = _agent(tmp_path)
    state = {
        "messages": [HumanMessage(content="换一个订单看看")],
        "user_id": "u1",
        "sub_results": {
            "intent_router": {"secondary": "order_query", "entities": {}},
            "_session_context": {
                "last_intent": "order_query",
                "accumulated_entities": {"order_id": "ORD-20260801-0001"},
            },
        },
    }

    out = await agent.process(state)

    assert "请提供演示订单号" in out["sub_results"]["ticket_handler"]
    with repository.transaction() as connection:
        assert connection.execute("SELECT COUNT(*) FROM support_tickets").fetchone()[0] == 0
