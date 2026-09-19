from __future__ import annotations

import pytest

from mcp.execution_ledger import ExecutionLedger
from mcp.mcp_server import MCPToolServer, create_default_tools
from mcp.order_repository import OrderRepository
from mcp.tool_execution import ToolExecutionContext, ToolExecutor
from tickets.service import canonical_ticket_payload_hash


def _fixture(tmp_path):
    repository = OrderRepository(str(tmp_path / "orders.db"))
    server = create_default_tools(MCPToolServer(), order_repository=repository)
    ledger = ExecutionLedger(repository.db_path)
    return repository, server, ToolExecutor(server, ledger=ledger)


@pytest.mark.asyncio
async def test_ticket_create_and_query_use_durable_service_and_ownership(tmp_path):
    repository, server, executor = _fixture(tmp_path)
    arguments = {
        "client_request_id": "client-mcp-1",
        "user_id": "user-1",
        "title": "服务投诉",
        "description": "需要人工处理",
        "priority": "high",
        "category": "complaint",
    }
    arguments["request_payload_hash"] = canonical_ticket_payload_hash(arguments)
    created = await executor.execute(
        "ticket_create",
        arguments,
        ToolExecutionContext(confirmed=True, idempotency_key="exec-mcp-1"),
    )
    assert created.success is True
    ticket_id = created.result["ticket_id"]
    assert created.result["client_request_id"] == "client-mcp-1"
    assert created.result["success"] is True

    query = await executor.execute(
        "ticket_query", {"ticket_id": ticket_id, "user_id": "user-1"}
    )
    denied = await executor.execute(
        "ticket_query", {"ticket_id": ticket_id, "user_id": "other-user"}
    )
    assert query.success is True and query.result["ticket_id"] == ticket_id
    assert denied.success is True and denied.result["success"] is False
    assert executor.ledger.get("query-key") is None
    with repository.transaction() as connection:
        assert connection.execute("SELECT COUNT(*) FROM support_tickets").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_tool_executor_failure_does_not_claim_success_or_create_ticket(tmp_path):
    _, server, executor = _fixture(tmp_path)
    server.get_tool("ticket_create").handler = lambda **_kwargs: (_ for _ in ()).throw(
        RuntimeError("ticket backend down")
    )
    result = await executor.execute(
        "ticket_create",
        {
            "client_request_id": "client-fail",
            "request_payload_hash": canonical_ticket_payload_hash(
                user_id="user-1", title="失败", description="后端失败"
            ),
            "user_id": "user-1",
            "title": "失败",
            "description": "后端失败",
        },
        ToolExecutionContext(confirmed=True, idempotency_key="exec-fail"),
    )
    assert result.success is False
    assert result.error_code == "execution_error"
    assert executor.ledger.get("exec-fail")["status"] == "failed"


def test_ticket_metadata_exposes_recovery_fields_neither_to_clients(tmp_path):
    _, server, _ = _fixture(tmp_path)
    tool = server.get_tool("ticket_create")
    public = next(item for item in server.list_tools() if item["name"] == "ticket_create")
    assert tool.recovery_fields == (
        "client_request_id",
        "user_id",
        "request_payload_hash",
    )
    assert "recovery_fields" not in public
    assert "request_payload_hash" in public["inputSchema"]["required"]
