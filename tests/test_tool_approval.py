from __future__ import annotations

import asyncio

import pytest

from mcp.approval_store import ApprovalService, ApprovalStateError
from mcp.execution_ledger import ExecutionLedger
from mcp.mcp_server import MCPToolServer, ToolDefinition
from mcp.tool_execution import ToolExecutionContext, ToolExecutor


def _server(
    *,
    name: str = "high_tool",
    risk_level: str = "high",
    operation_type: str = "write",
    handler=None,
) -> MCPToolServer:
    server = MCPToolServer()

    async def default_handler(value: int = 1) -> dict[str, int]:
        return {"value": value}

    server.register_tool(
        ToolDefinition(
            name=name,
            description="approval test tool",
            input_schema={"type": "object"},
            handler=handler or default_handler,
            operation_type=operation_type,
            risk_level=risk_level,
            requires_confirmation=operation_type == "write",
            retryable=False,
        )
    )
    return server


def _context(key: str, approval_id: str | None = None) -> ToolExecutionContext:
    return ToolExecutionContext(
        confirmed=True,
        idempotency_key=key,
        approval_id=approval_id,
    )


def test_approval_lifecycle_is_strict_and_db_backed(tmp_path) -> None:
    db_path = tmp_path / "approvals.db"
    service = ApprovalService(db_path)
    first = service.create_request("high_tool", {"value": 1}, requested_by="agent")
    second = service.create_request("high_tool", {"value": 2})

    assert first.approval_id == "APR-000001"
    assert second.approval_id == "APR-000002"
    assert service.get(first.approval_id).status == "pending"
    assert service.approve(first.approval_id, "human", "ok").status == "approved"
    with pytest.raises(ApprovalStateError):
        service.reject(first.approval_id, "human", "too late")
    assert service.reject(second.approval_id, "human", "no").status == "rejected"
    with pytest.raises(ApprovalStateError):
        service.approve(second.approval_id, "human")

    restarted = ApprovalService(db_path)
    third = restarted.create_request("high_tool", {"value": 3})
    assert third.approval_id == "APR-000003"
    assert restarted.get(first.approval_id).status == "approved"


@pytest.mark.asyncio
async def test_high_risk_requires_valid_approved_matching_single_use(tmp_path) -> None:
    db_path = tmp_path / "shared.db"
    service = ApprovalService(db_path)
    ledger = ExecutionLedger(db_path)
    calls = 0

    async def handler(value: int = 1) -> dict[str, int]:
        nonlocal calls
        calls += 1
        return {"value": value}

    executor = ToolExecutor(_server(handler=handler), ledger=ledger, approval_service=service)
    arguments = {"value": 1}
    pending = service.create_request("high_tool", arguments)

    assert (await executor.execute("high_tool", arguments, _context("pending", pending.approval_id))).error_code == "approval_invalid"
    rejected = service.create_request("high_tool", arguments)
    service.reject(rejected.approval_id, "human", "no")
    assert (await executor.execute("high_tool", arguments, _context("rejected", rejected.approval_id))).error_code == "approval_invalid"

    mismatch = service.create_request("high_tool", {"value": 2})
    service.approve(mismatch.approval_id, "human")
    assert (await executor.execute("high_tool", arguments, _context("mismatch", mismatch.approval_id))).error_code == "approval_invalid"
    assert calls == 0

    approved = service.create_request("high_tool", arguments)
    service.approve(approved.approval_id, "human")
    first = await executor.execute("high_tool", arguments, _context("approved-1", approved.approval_id))
    replay = await executor.execute("high_tool", arguments, _context("approved-1", approved.approval_id))
    reuse = await executor.execute("high_tool", arguments, _context("approved-2", approved.approval_id))
    assert first.success and replay.replayed
    assert reuse.error_code == "approval_invalid"
    assert service.get(approved.approval_id).status == "consumed"
    assert calls == 1


@pytest.mark.asyncio
async def test_high_risk_tool_binding_and_service_errors_do_not_run_handler(tmp_path) -> None:
    db_path = tmp_path / "binding.db"
    ledger = ExecutionLedger(db_path)
    service = ApprovalService(db_path)
    calls = 0

    async def handler() -> dict[str, bool]:
        nonlocal calls
        calls += 1
        return {"ok": True}

    server = _server(handler=handler)
    no_service = await ToolExecutor(server, ledger=ledger).execute(
        "high_tool", {}, ToolExecutionContext(confirmed=True, idempotency_key="no-service")
    )
    assert no_service.error_code == "approval_service_required"
    approval = service.create_request("other_tool", {})
    service.approve(approval.approval_id, "human")
    wrong_tool = await ToolExecutor(server, ledger=ledger, approval_service=service).execute(
        "high_tool", {}, _context("wrong-tool", approval.approval_id)
    )
    assert wrong_tool.error_code == "approval_invalid"
    assert calls == 0

    matching = service.create_request("high_tool", {})
    service.approve(matching.approval_id, "human")
    success = await ToolExecutor(server, ledger=ledger, approval_service=service).execute(
        "high_tool", {}, _context("matching", matching.approval_id)
    )
    assert success.success
    assert calls == 1

    missing = await ToolExecutor(server, ledger=ledger, approval_service=service).execute(
        "high_tool", {}, _context("missing")
    )
    assert missing.error_code == "approval_required"


@pytest.mark.asyncio
async def test_concurrent_consumption_allows_one_handler(tmp_path) -> None:
    db_path = tmp_path / "concurrent.db"
    service = ApprovalService(db_path)
    ledger = ExecutionLedger(db_path)
    approval = service.create_request("high_tool", {})
    service.approve(approval.approval_id, "human")
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def handler() -> dict[str, bool]:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return {"ok": True}

    executor = ToolExecutor(_server(handler=handler), ledger=ledger, approval_service=service)
    first_task = asyncio.create_task(
        executor.execute("high_tool", {}, _context("key-1", approval.approval_id))
    )
    await started.wait()
    second = await executor.execute("high_tool", {}, _context("key-2", approval.approval_id))
    release.set()
    first = await first_task

    assert first.success and second.error_code == "approval_invalid"
    assert calls == 1


@pytest.mark.asyncio
async def test_medium_write_and_read_do_not_require_approval_service(tmp_path) -> None:
    ledger = ExecutionLedger(tmp_path / "regression.db")
    medium = ToolExecutor(_server(risk_level="medium"), ledger=ledger)
    read = ToolExecutor(_server(risk_level="low", operation_type="read"))

    write_result = await medium.execute("high_tool", {}, _context("medium"))
    read_result = await read.execute("high_tool", {})
    assert write_result.success
    assert read_result.success
