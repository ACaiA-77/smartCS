from __future__ import annotations

import asyncio

import pytest

from mcp.mcp_server import MCPToolServer, ToolCallResult, ToolDefinition, create_default_tools
from mcp.execution_ledger import ExecutionLedger
from mcp.order_repository import OrderRepository
from mcp.tool_execution import (
    ToolExecutionContext,
    ToolExecutionPolicy,
    ToolExecutor,
)


def _registered_server(
    *,
    operation_type: str = "read",
    retryable: bool = True,
    requires_confirmation: bool = False,
    handler=None,
) -> MCPToolServer:
    server = MCPToolServer()

    async def default_handler() -> dict[str, bool]:
        return {"ok": True}

    server.register_tool(
        ToolDefinition(
            name="test_tool",
            description="test",
            input_schema={"type": "object"},
            handler=handler or default_handler,
            operation_type=operation_type,
            risk_level="medium" if operation_type == "write" else "low",
            requires_confirmation=requires_confirmation,
            retryable=retryable,
        )
    )
    return server


@pytest.mark.asyncio
async def test_confirmation_blocks_refund_create_without_db_write(tmp_path) -> None:
    repository = OrderRepository(str(tmp_path / "orders.db"))
    server = create_default_tools(MCPToolServer(), order_repository=repository)
    arguments = {
        "order_id": "ORD-20260801-0002",
        "user_id": "user_002",
        "reason": "需要退款",
    }
    before = repository.get_order(arguments["order_id"])["refunds"]

    result = await ToolExecutor(server).execute("refund_create", arguments)

    assert result.success is False
    assert result.status == "confirmation_required"
    assert result.error_code == "confirmation_required"
    assert result.attempts == 0
    assert result.operation_type == "write"
    assert result.risk_level == "medium"
    assert result.requires_confirmation is True
    assert repository.get_order(arguments["order_id"])["refunds"] == before


@pytest.mark.asyncio
async def test_confirmed_refund_create_executes_once(tmp_path) -> None:
    repository = OrderRepository(str(tmp_path / "orders.db"))
    server = create_default_tools(MCPToolServer(), order_repository=repository)
    ledger = ExecutionLedger(repository.db_path)

    result = await ToolExecutor(server, ledger=ledger).execute(
        "refund_create",
        {
            "order_id": "ORD-20260801-0002",
            "user_id": "user_002",
            "reason": "确认退款",
        },
        ToolExecutionContext(confirmed=True, idempotency_key="refund-1"),
    )

    assert result.success is True
    assert result.status == "completed"
    assert result.result["success"] is True
    assert result.attempts == 1
    assert len(repository.get_order("ORD-20260801-0002")["refunds"]) == 1


@pytest.mark.asyncio
async def test_write_retryable_metadata_still_does_not_retry(tmp_path) -> None:
    calls = 0

    async def handler() -> None:
        nonlocal calls
        calls += 1
        raise RuntimeError("write failed")

    server = _registered_server(
        operation_type="write",
        retryable=True,
        requires_confirmation=True,
        handler=handler,
    )
    ledger = ExecutionLedger(tmp_path / "ledger.db")
    result = await ToolExecutor(server, ledger=ledger).execute(
        "test_tool", {}, ToolExecutionContext(confirmed=True, idempotency_key="write-1")
    )

    assert result.success is False
    assert result.error_code == "execution_error"
    assert result.attempts == 1
    assert calls == 1


@pytest.mark.asyncio
async def test_retryable_read_retries_transient_exception() -> None:
    calls = 0

    async def handler() -> dict[str, bool]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("temporary")
        return {"ok": True}

    result = await ToolExecutor(
        _registered_server(handler=handler)
    ).execute("test_tool", {})

    assert result.success is True
    assert result.status == "completed"
    assert result.attempts == 2
    assert calls == 2


@pytest.mark.asyncio
async def test_non_retryable_read_does_not_retry() -> None:
    calls = 0

    async def handler() -> None:
        nonlocal calls
        calls += 1
        raise RuntimeError("permanent")

    result = await ToolExecutor(
        _registered_server(retryable=False, handler=handler)
    ).execute("test_tool", {})

    assert result.success is False
    assert result.attempts == 1
    assert calls == 1


@pytest.mark.asyncio
async def test_retryable_read_timeout_retries() -> None:
    calls = 0

    async def handler() -> None:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.05)

    result = await ToolExecutor(
        _registered_server(handler=handler),
        ToolExecutionPolicy(timeout_seconds=0.001),
    ).execute("test_tool", {})

    assert result.success is False
    assert result.status == "failed"
    assert result.error_code == "timeout"
    assert result.attempts == 2
    assert calls == 2


@pytest.mark.asyncio
async def test_write_timeout_does_not_retry(tmp_path) -> None:
    calls = 0

    async def handler() -> None:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.05)

    ledger = ExecutionLedger(tmp_path / "ledger.db")
    result = await ToolExecutor(
        _registered_server(
            operation_type="write",
            requires_confirmation=True,
            handler=handler,
        ),
        ToolExecutionPolicy(timeout_seconds=0.001),
        ledger=ledger,
    ).execute("test_tool", {}, ToolExecutionContext(confirmed=True, idempotency_key="timeout-1"))

    assert result.success is False
    assert result.error_code == "timeout"
    assert result.attempts == 1
    assert calls == 1


@pytest.mark.asyncio
async def test_business_failure_is_completed_without_retry() -> None:
    calls = 0

    async def handler() -> ToolCallResult:
        nonlocal calls
        calls += 1
        return ToolCallResult(
            tool_name="test_tool",
            success=True,
            result={"success": False, "reason_code": "not_eligible"},
        )

    result = await ToolExecutor(_registered_server(handler=handler)).execute("test_tool", {})

    assert result.success is True
    assert result.status == "completed"
    assert result.result["success"] is False
    assert result.attempts == 1
    assert calls == 1


@pytest.mark.asyncio
async def test_unknown_tool_is_structured_failure() -> None:
    result = await ToolExecutor(MCPToolServer()).execute("missing", {})

    assert result.success is False
    assert result.status == "failed"
    assert result.error_code == "tool_not_found"
    assert result.attempts == 0
    assert result.as_dict()["tool_name"] == "missing"


@pytest.mark.asyncio
async def test_knowledge_search_rejects_empty_query() -> None:
    server = create_default_tools(MCPToolServer())

    result = await ToolExecutor(server).execute("knowledge_search", {"query": "  "})

    assert result.success is False
    assert result.status == "failed"
    assert result.error_code == "execution_error"
    assert result.error == "query must not be empty"
