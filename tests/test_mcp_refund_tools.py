from __future__ import annotations

from datetime import datetime

import pytest

from mcp.mcp_server import MCPToolServer, create_default_tools
from mcp.order_repository import OrderRepository
from sandbox.business_simulator import BusinessSimulator


def _server(tmp_path):
    repository = OrderRepository(str(tmp_path / "orders.db"))
    return create_default_tools(MCPToolServer(), order_repository=repository), repository


def test_refund_tools_publish_metadata_and_category_filter(tmp_path) -> None:
    server, _ = _server(tmp_path)

    tools = {tool["name"]: tool for tool in server.list_tools()}
    assert tools["order_query"]["operationType"] == "read"
    assert tools["knowledge_search"]["riskLevel"] == "low"
    assert tools["risk_check"]["requiresConfirmation"] is False
    assert tools["ticket_create"]["operationType"] == "write"
    assert tools["ticket_create"]["riskLevel"] == "medium"
    assert tools["ticket_create"]["requiresConfirmation"] is True
    assert tools["ticket_create"]["retryable"] is False
    assert tools["refund_evaluate"]["operationType"] == "read"
    assert tools["refund_evaluate"]["riskLevel"] == "low"
    assert tools["refund_evaluate"]["requiresConfirmation"] is False
    assert tools["refund_evaluate"]["retryable"] is True
    assert tools["refund_create"]["operationType"] == "write"
    assert tools["refund_create"]["riskLevel"] == "medium"
    assert tools["refund_create"]["requiresConfirmation"] is True
    assert tools["refund_create"]["retryable"] is False

    refund_tools = server.list_tools(category="refund")
    assert {tool["name"] for tool in refund_tools} == {"refund_evaluate", "refund_create"}
    assert refund_tools[0]["inputSchema"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("order_id", "user_id", "eligible", "reason_code"),
    [
        ("ORD-20260801-0002", "user_002", True, "eligible"),
        ("ORD-20260801-0002", "user_001", False, "order_not_owned"),
        ("ORD-20260801-0001", "user_001", False, "not_paid"),
        ("ORD-20260801-0008", "user_008", False, "already_refunded"),
        ("ORD-20260801-0007", "user_007", False, "refund_already_pending"),
    ],
)
async def test_refund_evaluate_uses_refund_service(
    tmp_path, order_id, user_id, eligible, reason_code
) -> None:
    server, _ = _server(tmp_path)

    result = await server.call_tool(
        "refund_evaluate",
        {"order_id": order_id, "user_id": user_id},
    )

    assert result.success is True
    assert result.result["eligible"] is eligible
    assert result.result["reason_code"] == reason_code


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "error"),
    [
        ({"order_id": "   ", "user_id": "user_002"}, "order_id must not be empty"),
        ({"order_id": "ORD-20260801-0002", "user_id": "   "}, "user_id must not be empty"),
    ],
)
async def test_refund_evaluate_rejects_empty_input(tmp_path, arguments, error) -> None:
    server, _ = _server(tmp_path)

    result = await server.call_tool("refund_evaluate", arguments)

    assert result.success is False
    assert result.error == error


@pytest.mark.asyncio
async def test_refund_create_trims_input_and_returns_created_result(tmp_path) -> None:
    server, repository = _server(tmp_path)

    result = await server.call_tool(
        "refund_create",
        {
            "order_id": " ORD-20260801-0002 ",
            "user_id": " user_002 ",
            "reason": " 商品不符 ",
        },
    )

    assert result.success is True
    assert result.result["success"] is True
    assert result.result["reason_code"] == "created"
    refund = repository.get_order("ORD-20260801-0002")["refunds"][0]
    assert refund["reason"] == "商品不符"
    assert refund["status"] == "pending"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "error"),
    [
        (
            {"order_id": " ", "user_id": "user_002", "reason": "商品不符"},
            "order_id must not be empty",
        ),
        (
            {"order_id": "ORD-20260801-0002", "user_id": " ", "reason": "商品不符"},
            "user_id must not be empty",
        ),
        (
            {"order_id": "ORD-20260801-0002", "user_id": "user_002", "reason": " "},
            "reason must not be empty",
        ),
    ],
)
async def test_refund_create_rejects_empty_input(tmp_path, arguments, error) -> None:
    server, _ = _server(tmp_path)

    result = await server.call_tool("refund_create", arguments)

    assert result.success is False
    assert result.error == error


@pytest.mark.asyncio
async def test_refund_create_keeps_business_failure_inside_tool_success(tmp_path) -> None:
    server, _ = _server(tmp_path)

    result = await server.call_tool(
        "refund_create",
        {
            "order_id": "ORD-20260801-0002",
            "user_id": "user_001",
            "reason": "商品不符",
        },
    )

    assert result.success is True
    assert result.result["success"] is False
    assert result.result["reason_code"] == "order_not_owned"


@pytest.mark.asyncio
async def test_refund_tools_report_missing_service_as_tool_failure() -> None:
    server = create_default_tools(MCPToolServer())

    result = await server.call_tool(
        "refund_evaluate",
        {"order_id": "ORD-20260801-0002", "user_id": "user_002"},
    )

    assert result.success is False
    assert result.error == "refund service unavailable"


@pytest.mark.asyncio
async def test_refund_create_flows_through_business_simulator(tmp_path) -> None:
    server, repository = _server(tmp_path)

    created = await server.call_tool(
        "refund_create",
        {
            "order_id": "ORD-20260801-0002",
            "user_id": "user_002",
            "reason": "完成链路测试",
        },
    )
    refund_id = created.result["refund_id"]

    BusinessSimulator(repository).tick(
        now=datetime(2026, 9, 17, 12, 0, 0),
        max_transitions=1000,
    )

    order = repository.get_order("ORD-20260801-0002")
    refund = next(item for item in order["refunds"] if item["refund_id"] == refund_id)
    assert order["status"] == "refunded"
    assert order["payment"]["status"] == "paid"
    assert refund["status"] == "completed"
