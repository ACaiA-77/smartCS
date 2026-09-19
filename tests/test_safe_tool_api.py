from __future__ import annotations

import httpx
import pytest

import api.main as api_main
from auth.context import UserContext
from mcp.approval_store import ApprovalService
from mcp.execution_ledger import ExecutionLedger
from mcp.mcp_server import MCPToolServer, ToolDefinition, create_default_tools
from mcp.order_repository import OrderRepository
from mcp.tool_execution import ToolExecutor


@pytest.fixture
def api_runtime(tmp_path, monkeypatch):
    # Component-only identity fixture. Real JWT/MySQL isolation is covered in
    # test_user_sessions.py; never override authorization or tool policy here.
    monkeypatch.setitem(api_main.app.dependency_overrides, api_main.get_current_user,
                        lambda: UserContext(account_id=1, username="component", business_user_id="user_002"))
    repository = OrderRepository(str(tmp_path / "orders.db"))
    server = create_default_tools(MCPToolServer(), order_repository=repository)
    ledger = ExecutionLedger(repository.db_path)
    approvals = ApprovalService(repository.db_path)
    executor = ToolExecutor(server, ledger=ledger, approval_service=approvals)
    monkeypatch.setattr(api_main, "order_repository", repository)
    monkeypatch.setattr(api_main, "mcp_server", server)
    monkeypatch.setattr(api_main, "execution_ledger", ledger)
    monkeypatch.setattr(api_main, "approval_service", approvals)
    monkeypatch.setattr(api_main, "tool_executor", executor)
    return repository, server, approvals


async def request(method: str, path: str, **kwargs) -> httpx.Response:
    transport = httpx.ASGITransport(app=api_main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.request(method, path, **kwargs)


@pytest.mark.asyncio
async def test_customer_read_succeeds_and_legacy_write_is_rejected(api_runtime):
    repository, _, _ = api_runtime
    read = await request(
        "POST",
        "/api/tools/call",
        json={"name": "order_query", "arguments": {"order_id": "ORD-20260801-0002"}},
    )
    assert read.status_code == 200
    assert read.json()["success"] is True
    assert read.json()["result"]["found"] is True

    before = len(repository.get_order("ORD-20260801-0002")["refunds"])
    write = await request(
        "POST",
        "/api/tools/call",
        json={
            "name": "refund_create",
            "arguments": {
                "order_id": "ORD-20260801-0002",
                "user_id": "user_002",
                "reason": "API legacy rejection",
            },
        },
    )
    assert write.status_code == 403
    assert len(repository.get_order("ORD-20260801-0002")["refunds"]) == before


@pytest.mark.asyncio
async def test_execute_order_query_returns_structured_result(api_runtime):
    api_runtime
    response = await request(
        "POST",
        "/api/tools/execute",
        json={"name": "order_query", "arguments": {"order_id": "ORD-20260801-0002"}},
    )
    body = response.json()
    assert response.status_code == 200
    assert body["status"] == "completed"
    assert body["tool_name"] == "order_query"
    assert {"success", "error_code", "error", "result", "attempts", "duration_ms", "operation_type", "risk_level", "requires_confirmation", "replayed"}.issubset(body)


@pytest.mark.asyncio
async def test_customer_cannot_self_authorize_refund_write(api_runtime):
    repository, _, _ = api_runtime
    arguments = {
        "order_id": "ORD-20260801-0002",
        "user_id": "user_002",
        "reason": "API refund",
    }
    before = len(repository.get_order(arguments["order_id"])["refunds"])
    unconfirmed = await request(
        "POST",
        "/api/tools/execute",
        json={"name": "refund_create", "arguments": arguments},
    )
    assert unconfirmed.status_code == 403
    assert len(repository.get_order(arguments["order_id"])["refunds"]) == before

    payload = {
        "name": "refund_create",
        "arguments": arguments,
        "confirmed": True,
        "idempotency_key": "api-refund-1",
    }
    first = await request("POST", "/api/tools/execute", json=payload)
    replay = await request("POST", "/api/tools/execute", json=payload)
    conflict = await request(
        "POST",
        "/api/tools/execute",
        json={**payload, "arguments": {**arguments, "reason": "different"}},
    )
    assert first.status_code == replay.status_code == conflict.status_code == 403
    assert len(repository.get_order(arguments["order_id"])["refunds"]) == before
    assert api_main.execution_ledger.get("api-refund-1") is None


@pytest.mark.asyncio
async def test_customer_cannot_create_read_or_decide_approvals(api_runtime):
    _, _, approvals = api_runtime
    approval = approvals.create_request("high_tool", {"value": 1}, "internal-test")
    created = await request(
        "POST",
        "/api/approvals",
        json={"tool_name": "high_tool", "arguments": {"value": 1}, "requested_by": "agent"},
    )
    approval_id = approval.approval_id
    assert created.status_code == 403

    approved = await request(
        "POST",
        f"/api/approvals/{approval_id}/approve",
        json={"decided_by": "human", "reason": "ok"},
    )
    fetched = await request("GET", f"/api/approvals/{approval_id}")
    invalid_reject = await request(
        "POST",
        f"/api/approvals/{approval_id}/reject",
        json={"decided_by": "human", "reason": "too late"},
    )
    assert approved.status_code == fetched.status_code == invalid_reject.status_code == 403
    assert approvals.get(approval_id).status == "pending"


@pytest.mark.asyncio
async def test_customer_cannot_invoke_unlisted_tools_even_with_confirmation(api_runtime):
    _, server, _ = api_runtime

    async def handler(value: int = 1) -> dict[str, int]:
        pytest.fail("customer invoked an unlisted tool")

    server.register_tool(
        ToolDefinition(
            name="synthetic_high_risk",
            description="test-only high risk tool",
            input_schema={"type": "object"},
            handler=handler,
            operation_type="read",
            risk_level="high",
            requires_confirmation=True,
            retryable=False,
        )
    )
    missing = await request(
        "POST",
        "/api/tools/execute",
        json={"name": "synthetic_high_risk", "arguments": {"value": 7}, "confirmed": True},
    )
    completed = await request(
        "POST",
        "/api/tools/execute",
        json={
            "name": "synthetic_high_risk",
            "arguments": {"value": 7},
            "confirmed": True,
            "approval_id": "caller-cannot-grant-approval",
        },
    )
    assert missing.status_code == completed.status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["call", "execute"])
async def test_tool_identity_spoof_is_rejected(api_runtime, endpoint):
    response = await request("POST", f"/api/tools/{endpoint}", json={
        "name": "order_query",
        "arguments": {"order_id": "ORD-20260801-0001", "user_id": "user_001"},
    })
    assert response.status_code == 403
    assert "ORD-20260801-0001" not in response.text
