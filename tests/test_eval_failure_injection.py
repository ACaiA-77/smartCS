from __future__ import annotations

import math

import pytest

from evals.faults import FaultInjectingHandler, FaultPlan
from evals.scenarios import build_runtime, inject
from mcp.mcp_server import MCPToolServer, ToolDefinition
from mcp.tool_execution import ToolExecutionContext, ToolExecutionPolicy, ToolExecutor


@pytest.mark.asyncio
async def test_raise_once_injects_once_then_passes_through() -> None:
    calls = 0

    async def handler() -> dict[str, bool]:
        nonlocal calls
        calls += 1
        return {"ok": True}

    wrapped = FaultInjectingHandler(handler, FaultPlan.raise_once())
    with pytest.raises(RuntimeError):
        await wrapped()
    assert await wrapped() == {"ok": True}
    assert (wrapped.call_count, wrapped.fault_count, calls) == (2, 1, 1)


@pytest.mark.asyncio
async def test_raise_always_never_silently_succeeds() -> None:
    async def handler() -> None:
        raise AssertionError("must not run")

    wrapped = FaultInjectingHandler(handler, FaultPlan.raise_always())
    with pytest.raises(RuntimeError):
        await wrapped()
    with pytest.raises(RuntimeError):
        await wrapped()
    assert (wrapped.call_count, wrapped.fault_count) == (2, 2)


def test_fault_plan_rejects_unknown_mode_and_invalid_delay() -> None:
    with pytest.raises(ValueError):
        FaultPlan("unknown")
    with pytest.raises(ValueError):
        FaultPlan.delay(-0.1)
    with pytest.raises(ValueError):
        FaultPlan("delay", delay_seconds=math.inf)


@pytest.mark.asyncio
async def test_delay_triggers_real_tool_executor_timeout() -> None:
    server = MCPToolServer()

    async def handler() -> dict[str, bool]:
        return {"ok": True}

    wrapped = FaultInjectingHandler(handler, FaultPlan.delay(0.05))
    server.register_tool(
        ToolDefinition(
            name="read",
            description="read",
            input_schema={"type": "object"},
            handler=wrapped,
            retryable=True,
        )
    )
    result = await ToolExecutor(
        server, ToolExecutionPolicy(timeout_seconds=0.001, max_read_attempts=2)
    ).execute("read", {})
    assert result.error_code == "timeout"
    assert result.attempts == 2
    assert (wrapped.call_count, wrapped.fault_count) == (2, 2)


@pytest.mark.asyncio
async def test_read_transient_failure_retries_but_write_failure_does_not() -> None:
    runtime = build_runtime()
    try:
        read_fault = inject(runtime, "order_query", FaultPlan.raise_once())
        await runtime.executor.execute("order_query", {"order_id": "ORD-20260801-0002", "user_id": "user_002"})
        read_result = runtime.executor.results_for("order_query")[-1]
        assert read_result.attempts == 2
        assert read_fault.call_count == 2

        write_fault = inject(runtime, "ticket_create", FaultPlan.raise_always())
        arguments = {
            "client_request_id": "eval-write",
            "request_payload_hash": "hash",
            "user_id": "user_002",
            "title": "title",
            "description": "description",
        }
        result = await runtime.actual_executor.execute(
            "ticket_create",
            arguments,
            ToolExecutionContext(confirmed=True, idempotency_key="eval-write-key"),
        )
        assert result.success is False
        assert result.attempts == 1
        assert write_fault.call_count == 1
        assert runtime.ledger.get("eval-write-key")["status"] == "failed"
    finally:
        runtime.close()
