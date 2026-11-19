"""Public /api/tools/* safety boundary tests for Personal Copilot."""

from __future__ import annotations

import httpx
import pytest

import api.main as api_main
from auth.context import UserContext
from knowledge_engine import ObsidianVault, OntologyStore
from tool_runtime.approval_store import ApprovalService
from tool_runtime.execution_ledger import ExecutionLedger
from tool_runtime.mcp_server import MCPToolServer, ToolDefinition, create_default_tools
from tool_runtime.tool_execution import ToolExecutor


@pytest.fixture
def api_runtime(tmp_path, monkeypatch):
    monkeypatch.setitem(
        api_main.app.dependency_overrides,
        api_main.get_current_user,
        lambda: UserContext(account_id=1, username="copilot", business_user_id="user_002"),
    )
    vault = ObsidianVault(tmp_path / "vault")
    vault.write_wiki_page("alpha", "Project Alpha", "Alpha roadmap note", tags=["alpha"])
    ontology = OntologyStore(tmp_path / "ontology.sqlite")
    server = create_default_tools(MCPToolServer(), vault=vault, ontology=ontology)
    db_path = str(tmp_path / "copilot.db")
    ledger = ExecutionLedger(db_path)
    approvals = ApprovalService(db_path)
    executor = ToolExecutor(server, ledger=ledger, approval_service=approvals)
    monkeypatch.setattr(api_main, "mcp_server", server)
    monkeypatch.setattr(api_main, "execution_ledger", ledger)
    monkeypatch.setattr(api_main, "approval_service", approvals)
    monkeypatch.setattr(api_main, "tool_executor", executor)
    yield vault, server, approvals
    ontology.close()


async def request(method: str, path: str, **kwargs) -> httpx.Response:
    transport = httpx.ASGITransport(app=api_main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.request(method, path, **kwargs)


@pytest.mark.asyncio
async def test_public_read_succeeds_and_write_tool_is_rejected(api_runtime):
    vault, _, _ = api_runtime
    read = await request(
        "POST",
        "/api/tools/call",
        json={"name": "vault_search", "arguments": {"query": "Alpha"}},
    )
    assert read.status_code == 200
    assert read.json()["success"] is True
    assert read.json()["result"][0]["title"] == "Project Alpha"

    before_daily = list((vault.root / "daily").glob("*.md"))
    write = await request(
        "POST",
        "/api/tools/call",
        json={"name": "vault_append_daily", "arguments": {"entry": "forbidden direct call"}},
    )
    assert write.status_code == 403
    assert list((vault.root / "daily").glob("*.md")) == before_daily


@pytest.mark.asyncio
async def test_execute_vault_search_returns_structured_result(api_runtime):
    response = await request(
        "POST",
        "/api/tools/execute",
        json={"name": "vault_search", "arguments": {"query": "Alpha"}},
    )
    body = response.json()
    assert response.status_code == 200
    assert body["status"] == "completed"
    assert body["tool_name"] == "vault_search"
    assert {
        "success",
        "error_code",
        "error",
        "result",
        "attempts",
        "duration_ms",
        "operation_type",
        "risk_level",
        "requires_confirmation",
        "replayed",
    }.issubset(body)


@pytest.mark.asyncio
async def test_caller_cannot_self_authorize_copilot_write(api_runtime):
    vault, _, _ = api_runtime
    arguments = {"entry": "API bypass attempt", "date_str": "2026-11-18"}
    unconfirmed = await request(
        "POST",
        "/api/tools/execute",
        json={"name": "vault_append_daily", "arguments": arguments},
    )
    assert unconfirmed.status_code == 403

    payload = {
        "name": "vault_append_daily",
        "arguments": arguments,
        "confirmed": True,
        "idempotency_key": "api-daily-1",
    }
    first = await request("POST", "/api/tools/execute", json=payload)
    replay = await request("POST", "/api/tools/execute", json=payload)
    conflict = await request(
        "POST",
        "/api/tools/execute",
        json={**payload, "arguments": {**arguments, "entry": "different"}},
    )
    assert first.status_code == replay.status_code == conflict.status_code == 403
    assert not (vault.root / "daily" / "2026-11-18.md").exists()
    assert api_main.execution_ledger.get("api-daily-1") is None


@pytest.mark.asyncio
async def test_caller_cannot_create_read_or_decide_approvals(api_runtime):
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
async def test_caller_cannot_invoke_unlisted_tools_even_with_confirmation(api_runtime):
    _, server, _ = api_runtime

    async def handler(value: int = 1) -> dict[str, int]:
        pytest.fail("caller invoked an unlisted tool")

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
    response = await request(
        "POST",
        f"/api/tools/{endpoint}",
        json={
            "name": "vault_search",
            "arguments": {"query": "Alpha", "user_id": "user_001"},
        },
    )
    assert response.status_code == 403
    assert "Project Alpha" not in response.text
