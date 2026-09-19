"""Authenticated tool boundaries use real SQLite business effects and ledger rows."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from auth.context import UserContext, current_user
from checkpoint.models import active_checkpoint
from mcp.execution_ledger import ExecutionLedger
from mcp.mcp_server import MCPToolServer, create_default_tools
from mcp.order_repository import OrderRepository
from mcp.tool_execution import ToolExecutionContext, ToolExecutor
from tickets.service import canonical_ticket_payload_hash


ORDER_A = "ORD-20260801-0021"  # Paid/processing order owned by user_001.
ORDER_B = "ORD-20260801-0002"
CUSTOMER_TOOLS = ("order_query", "refund_evaluate", "refund_create", "ticket_create", "ticket_query")


@pytest.fixture
def business(tmp_path):
    repository = OrderRepository(str(tmp_path / "business.db"))
    server = create_default_tools(MCPToolServer(), order_repository=repository)
    executor = ToolExecutor(server, ledger=ExecutionLedger(repository.db_path))
    return repository, server, executor


@contextmanager
def _as_user(user_id):
    token = current_user.set(UserContext(
        account_id=int(user_id.rsplit("_", 1)[-1]),
        username=f"customer-{user_id}",
        business_user_id=user_id,
    ))
    try:
        yield
    finally:
        current_user.reset(token)


def _ticket_arguments(user_id="user_001"):
    arguments = {
        "client_request_id": "private-ticket-request",
        "user_id": user_id,
        "title": "private-ticket-title",
        "description": "private-ticket-description",
    }
    arguments["request_payload_hash"] = canonical_ticket_payload_hash(arguments)
    return arguments


async def _call(business, entry, name, arguments, key="isolation-write"):
    _, server, executor = business
    if entry == "mcp":
        return await server.call_tool(name, arguments)
    return await executor.execute(
        name, arguments, ToolExecutionContext(confirmed=True, idempotency_key=key)
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["mcp", "executor"])
@pytest.mark.parametrize("name", CUSTOMER_TOOLS)
async def test_model_cannot_override_authenticated_identity(business, entry, name):
    repository, server, executor = business
    handler = AsyncMock(return_value={"unexpected": True})
    server.get_tool(name).handler = handler
    arguments = {"user_id": "user_001"}

    with _as_user("user_002"):
        result = await _call(business, entry, name, arguments)

    assert result.success is False
    assert result.result is None
    assert result.error == "tool user_id does not match authenticated user"
    handler.assert_not_awaited()
    assert executor.ledger.get("isolation-write") is None
    assert repository.get_order(ORDER_A)["refunds"] == []
    assert arguments == {"user_id": "user_001"}


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["mcp", "executor"])
@pytest.mark.parametrize("name", CUSTOMER_TOOLS)
async def test_missing_identity_is_injected_without_mutating_model_arguments(business, entry, name):
    _, server, _ = business
    handler = AsyncMock(return_value={"ok": True})
    server.get_tool(name).handler = handler
    arguments = {}
    checkpoint = SimpleNamespace(before_write=AsyncMock(), after_write=Mock())
    token = active_checkpoint.set(checkpoint)
    try:
        with _as_user("user_002"):
            result = await _call(business, entry, name, arguments)
    finally:
        active_checkpoint.reset(token)

    assert result.success is True
    handler.assert_awaited_once_with(user_id="user_002")
    assert arguments == {}
    if entry == "executor" and name in {"refund_create", "ticket_create"}:
        assert checkpoint.before_write.await_args.args[1]["user_id"] == "user_002"
        checkpoint.after_write.assert_called_once_with(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["mcp", "executor"])
async def test_authenticated_order_query_returns_no_other_user_details(business, entry):
    with _as_user("user_002"):
        denied = await _call(business, entry, "order_query", {"order_id": ORDER_A})
        missing = await _call(business, entry, "order_query", {"order_id": "missing-order"})
        own = await _call(business, entry, "order_query", {"order_id": ORDER_B})

    assert denied.success is True
    assert denied.result == {**missing.result, "order_id": ORDER_A}
    assert denied.result["found"] is False
    assert own.result["found"] is True
    assert own.result["order_id"] == ORDER_B


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["mcp", "executor"])
async def test_cross_user_refund_has_no_effect_or_payment_metadata(business, entry):
    repository, _, _ = business
    before = repository.get_order(ORDER_A)
    with repository.transaction() as connection:
        count_before = connection.execute("SELECT COUNT(*) FROM refunds").fetchone()[0]

    with _as_user("user_002"):
        evaluated = await _call(business, entry, "refund_evaluate", {"order_id": ORDER_A})
        created = await _call(
            business, entry, "refund_create", {"order_id": ORDER_A, "reason": "not my order"}
        )

    assert evaluated.result["eligible"] is False
    assert created.result["success"] is False
    for result in (evaluated, created):
        assert result.result["reason_code"] == "order_not_owned"
        assert result.result["amount"] is None
        assert result.result["payment_id"] is None
        assert result.result.get("refund_id") is None
    assert repository.get_order(ORDER_A) == before
    with repository.transaction() as connection:
        assert connection.execute("SELECT COUNT(*) FROM refunds").fetchone()[0] == count_before


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["mcp", "executor"])
async def test_ticket_query_and_request_conflict_never_leak_another_users_metadata(business, entry):
    repository, _, _ = business
    with _as_user("user_001"):
        created = await _call(business, entry, "ticket_create", _ticket_arguments(), "ticket-A")
        ticket_id = created.result["ticket_id"]
        own = await _call(business, entry, "ticket_query", {"ticket_id": ticket_id})
        assert own.result["title"] == "private-ticket-title"

    with _as_user("user_002"):
        denied = await _call(business, entry, "ticket_query", {"ticket_id": ticket_id})
        conflict = await _call(
            business, entry, "ticket_create", _ticket_arguments("user_002"), "ticket-B"
        )

    assert denied.result == {"success": False, "reason_code": "ticket_not_found", "ticket_id": ticket_id}
    assert conflict.result == {
        "success": False,
        "reason_code": "client_request_conflict",
        "client_request_id": "private-ticket-request",
    }
    with repository.transaction() as connection:
        assert connection.execute("SELECT COUNT(*) FROM support_tickets").fetchone()[0] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["refund_create", "ticket_create"])
async def test_identity_guard_precedes_checkpoint_hook_and_ledger_replay(business, name):
    repository, _, executor = business
    arguments = _ticket_arguments() if name == "ticket_create" else {
        "order_id": ORDER_A, "user_id": "user_001", "reason": "own refund",
    }
    with _as_user("user_001"):
        created = await _call(business, "executor", name, arguments)
        replay = await _call(business, "executor", name, arguments)
    assert created.result["success"] is True
    assert replay.replayed is True and replay.result == created.result
    ledger_before = executor.ledger.get("isolation-write")
    checkpoint = SimpleNamespace(before_write=AsyncMock(), after_write=Mock())
    token = active_checkpoint.set(checkpoint)
    try:
        with _as_user("user_002"):
            denied = await _call(business, "executor", name, arguments)
    finally:
        active_checkpoint.reset(token)

    assert denied.error_code == "user_identity_mismatch"
    assert denied.result is None and denied.replayed is False and denied.attempts == 0
    checkpoint.before_write.assert_not_awaited()
    checkpoint.after_write.assert_not_called()
    with _as_user("user_002"):
        missing_identity = {key: value for key, value in arguments.items() if key != "user_id"}
        conflict = await _call(business, "executor", name, missing_identity)
    assert conflict.error_code == "idempotency_conflict"
    assert conflict.result is None and conflict.replayed is False
    assert executor.ledger.get("isolation-write") == ledger_before
    if name == "refund_create":
        assert len(repository.get_order(ORDER_A)["refunds"]) == 1
    else:
        with repository.transaction() as connection:
            assert connection.execute("SELECT COUNT(*) FROM support_tickets").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_concurrent_customer_contexts_do_not_cross_contaminate(business):
    async def query(user_id, order_id):
        with _as_user(user_id):
            await asyncio.sleep(0)
            return await _call(business, "executor", "order_query", {"order_id": order_id})

    first, second = await asyncio.gather(query("user_001", ORDER_A), query("user_002", ORDER_B))
    assert first.result["found"] is True and first.result["order_id"] == ORDER_A
    assert second.result["found"] is True and second.result["order_id"] == ORDER_B
    assert current_user.get() is None
