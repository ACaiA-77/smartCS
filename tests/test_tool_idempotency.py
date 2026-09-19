from __future__ import annotations

import asyncio

import pytest

from mcp.execution_ledger import ExecutionLedger
from mcp.mcp_server import MCPToolServer, ToolDefinition, create_default_tools
from mcp.order_repository import OrderRepository
from mcp.tool_execution import ToolExecutionContext, ToolExecutor


def _write_server(handler, name: str = "write_tool") -> MCPToolServer:
    server = MCPToolServer()
    server.register_tool(
        ToolDefinition(
            name=name,
            description="test write",
            input_schema={"type": "object"},
            handler=handler,
            operation_type="write",
            risk_level="medium",
            requires_confirmation=True,
            retryable=True,
        )
    )
    return server


def _context(key: str | None) -> ToolExecutionContext:
    return ToolExecutionContext(confirmed=True, idempotency_key=key)


@pytest.mark.asyncio
async def test_refund_duplicate_replays_and_writes_one_refund(tmp_path) -> None:
    repository = OrderRepository(str(tmp_path / "orders.db"))
    ledger = ExecutionLedger(repository.db_path)
    executor = ToolExecutor(
        create_default_tools(MCPToolServer(), order_repository=repository), ledger=ledger
    )
    arguments = {
        "order_id": "ORD-20260801-0002",
        "user_id": "user_002",
        "reason": "重复请求",
    }

    first = await executor.execute("refund_create", arguments, _context("refund-1"))
    second = await executor.execute("refund_create", arguments, _context("refund-1"))

    assert first.success is True and first.replayed is False
    assert second.success is True and second.replayed is True
    assert second.attempts == 0
    assert second.result == first.result
    assert len(repository.get_order(arguments["order_id"])["refunds"]) == 1


@pytest.mark.asyncio
async def test_restart_replays_same_db_without_new_handler_call(tmp_path) -> None:
    calls = 0

    async def handler(value: int) -> dict[str, int]:
        nonlocal calls
        calls += 1
        return {"value": value}

    db_path = tmp_path / "ledger.db"
    first = ToolExecutor(_write_server(handler), ledger=ExecutionLedger(db_path))
    first_result = await first.execute("write_tool", {"value": 3}, _context("restart-1"))
    second = ToolExecutor(_write_server(handler), ledger=ExecutionLedger(db_path))
    second_result = await second.execute("write_tool", {"value": 3}, _context("restart-1"))

    assert first_result.result == second_result.result == {"value": 3}
    assert second_result.replayed is True and second_result.attempts == 0
    assert calls == 1


@pytest.mark.asyncio
async def test_payload_and_tool_name_conflicts_do_not_run_handler(tmp_path) -> None:
    calls: list[str] = []

    async def handler(value: int) -> dict[str, int]:
        calls.append(str(value))
        return {"value": value}

    ledger = ExecutionLedger(tmp_path / "ledger.db")
    server = _write_server(handler, "write_a")
    server.register_tool(
        ToolDefinition(
            name="write_b",
            description="test write",
            input_schema={"type": "object"},
            handler=handler,
            operation_type="write",
            requires_confirmation=True,
        )
    )
    executor = ToolExecutor(server, ledger=ledger)

    assert (await executor.execute("write_a", {"value": 1}, _context("conflict"))).success
    payload_conflict = await executor.execute("write_a", {"value": 2}, _context("conflict"))
    tool_conflict = await executor.execute("write_b", {"value": 1}, _context("conflict"))

    assert payload_conflict.error_code == "idempotency_conflict"
    assert tool_conflict.error_code == "idempotency_conflict"
    assert calls == ["1"]


@pytest.mark.asyncio
async def test_failed_write_is_recorded_and_replayed_without_second_handler(tmp_path) -> None:
    calls = 0

    async def handler() -> None:
        nonlocal calls
        calls += 1
        raise RuntimeError("boom")

    ledger = ExecutionLedger(tmp_path / "ledger.db")
    executor = ToolExecutor(_write_server(handler), ledger=ledger)
    first = await executor.execute("write_tool", {}, _context("failed-1"))
    second = await executor.execute("write_tool", {}, _context("failed-1"))

    assert first.error_code == second.error_code == "execution_error"
    assert second.replayed is True and second.attempts == 0
    assert calls == 1
    assert ledger.get("failed-1")["status"] == "failed"


@pytest.mark.asyncio
async def test_business_failure_is_completed_and_replayed(tmp_path) -> None:
    calls = 0

    async def handler() -> dict[str, object]:
        nonlocal calls
        calls += 1
        return {"success": False, "reason_code": "not_eligible"}

    ledger = ExecutionLedger(tmp_path / "ledger.db")
    executor = ToolExecutor(_write_server(handler), ledger=ledger)
    first = await executor.execute("write_tool", {}, _context("business-1"))
    second = await executor.execute("write_tool", {}, _context("business-1"))

    assert first.success is True and first.result["success"] is False
    assert second.success is True and second.replayed is True
    assert ledger.get("business-1")["status"] == "completed"
    assert calls == 1


@pytest.mark.asyncio
async def test_concurrent_same_key_has_one_handler_and_one_in_progress_result(tmp_path) -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def handler() -> dict[str, bool]:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return {"ok": True}

    db_path = tmp_path / "ledger.db"
    executor_one = ToolExecutor(_write_server(handler), ledger=ExecutionLedger(db_path))
    executor_two = ToolExecutor(_write_server(handler), ledger=ExecutionLedger(db_path))
    first_task = asyncio.create_task(
        executor_one.execute("write_tool", {}, _context("concurrent-1"))
    )
    await started.wait()
    second = await executor_two.execute("write_tool", {}, _context("concurrent-1"))
    release.set()
    first = await first_task

    assert second.error_code == "execution_in_progress"
    assert first.success is True
    assert calls == 1


@pytest.mark.asyncio
async def test_key_normalization_required_and_length(tmp_path) -> None:
    async def handler() -> dict[str, bool]:
        return {"ok": True}

    ledger = ExecutionLedger(tmp_path / "ledger.db")
    executor = ToolExecutor(_write_server(handler), ledger=ledger)
    normalized = await executor.execute("write_tool", {}, _context("  normalized  "))
    empty = await executor.execute("write_tool", {}, _context("  "))
    missing = await executor.execute("write_tool", {}, _context(None))
    too_long = await executor.execute("write_tool", {}, _context("x" * 129))

    assert normalized.success is True
    assert ledger.get("normalized") is not None
    assert empty.error_code == "idempotency_key_required"
    assert missing.error_code == "idempotency_key_required"
    assert too_long.error_code == "invalid_idempotency_key"


@pytest.mark.asyncio
async def test_write_requires_ledger_but_read_does_not(tmp_path) -> None:
    async def write_handler() -> dict[str, bool]:
        return {"ok": True}

    write_result = await ToolExecutor(_write_server(write_handler)).execute(
        "write_tool", {}, _context("no-ledger")
    )

    read_server = MCPToolServer()

    async def read_handler() -> dict[str, bool]:
        return {"ok": True}

    read_server.register_tool(
        ToolDefinition("read_tool", "test read", {"type": "object"}, read_handler)
    )
    read_result = await ToolExecutor(read_server).execute("read_tool", {})

    assert write_result.error_code == "execution_ledger_required"
    assert read_result.success is True


@pytest.mark.asyncio
async def test_non_serializable_result_is_failed_and_replayable(tmp_path) -> None:
    async def handler() -> object:
        return object()

    ledger = ExecutionLedger(tmp_path / "ledger.db")
    executor = ToolExecutor(_write_server(handler), ledger=ledger)
    first = await executor.execute("write_tool", {}, _context("serialize-1"))
    second = await executor.execute("write_tool", {}, _context("serialize-1"))

    assert first.error_code == "result_not_serializable"
    assert second.error_code == "result_not_serializable"
    assert second.replayed is True and second.attempts == 0
    assert ledger.get("serialize-1")["status"] == "failed"


def test_ledger_get_includes_decoded_result(tmp_path) -> None:
    ledger = ExecutionLedger(tmp_path / "ledger.db")
    assert ledger.claim("inspect-1", "write_tool", {"value": 1}).status == "claimed"
    result = {"success": False, "status": "failed", "error_code": "x", "attempts": 1}
    ledger.fail("inspect-1", result)
    record = ledger.get("inspect-1")

    assert record["status"] == "failed"
    assert record["result"] == result
