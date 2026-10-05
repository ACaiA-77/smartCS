"""Phase 2 acceptance for POST /internal/tools/execute (design §2).

Real router, real MySQL, real MCP server + ToolExecutor over a seeded temp
SQLite demo database. Nothing is mocked: P2-7 asserts the internal channel's
result deep-equals a direct ToolExecutor call.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from auth.context import UserContext, current_user
from mcp.tool_execution import ToolExecutionContext
from tests.internal_api_helpers import (
    SERVICE_SECRET,
    TEST_DATABASE,
    USER_SECRET,
    apply_migration,
    build_app,
    build_tool_executor,
    random_username,
    seed_account,
    seed_session,
    service_header,
    user_token,
)

OWN_ORDER = "ORD-20260801-0001"      # belongs to user_001
OTHERS_ORDER = "ORD-20260801-0002"   # belongs to user_002


@pytest.fixture(autouse=True)
def _environment(monkeypatch):
    monkeypatch.setenv("INTERNAL_SERVICE_JWT_SECRET", SERVICE_SECRET)
    monkeypatch.setenv("AUTH_JWT_SECRET", USER_SECRET)
    monkeypatch.setenv("MYSQL_DATABASE", TEST_DATABASE)


@pytest_asyncio.fixture
async def harness(tmp_path):
    """An app wired to a real ToolExecutor, plus the account/session to call it."""
    apply_migration()
    executor, repository = build_tool_executor(tmp_path)
    app = await build_app(tool_executor=executor)
    account_id = seed_account(random_username("tools"), "user_001")
    session_id = "sess-tools"
    seed_session(session_id, account_id, "pi")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        yield {
            "http": http,
            "executor": executor,
            "repository": repository,
            "account_id": account_id,
            "session_id": session_id,
            "business_user_id": "user_001",
        }


def _call(harness, tool, arguments, *, client_request_id="req-1", **token_kwargs):
    headers = service_header(
        account_id=harness["account_id"],
        session_id=harness["session_id"],
        business_user_id=token_kwargs.pop("business_user_id", harness["business_user_id"]),
        client_request_id=client_request_id,
        **token_kwargs,
    )
    return harness["http"].post(
        "/internal/tools/execute",
        json={
            "tool": tool,
            "arguments": arguments,
            "session_id": harness["session_id"],
            "client_request_id": client_request_id,
        },
        headers=headers,
    )


@pytest.mark.asyncio
async def test_order_query_returns_real_business_data(harness):
    response = await _call(harness, "order_query", {"order_id": OWN_ORDER})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert body["details"]["success"] is True
    result = body["details"]["result"]
    assert result["found"] is True
    assert result["order_id"] == OWN_ORDER
    assert result["status_label"] == "待付款"
    assert result["data_source"] == "SQLite 本地国内电商演示数据"
    # Content is the bounded rendering that enters the model context.
    assert OWN_ORDER in body["content"]
    assert body["executor"]["retries"] == 0


@pytest.mark.asyncio
async def test_identity_in_arguments_is_stripped_audited_and_force_bound(harness):
    """P2-2: the model cannot speak for another user."""
    response = await _call(
        harness,
        "order_query",
        {"order_id": OTHERS_ORDER, "user_id": "user_002"},
    )
    assert response.status_code == 200, response.text
    body = response.json()

    # The forged identity never reached the executor...
    assert body["details"]["audit"]["strippedFields"] == ["user_id"]
    assert body["details"]["audit"]["forcedFields"] == ["user_id"]

    # ...and the query was answered for the AUTHENTICATED user, so another
    # user's order is simply not visible to them.
    assert body["details"]["result"]["found"] is False
    assert body["details"]["result"]["order_id"] == OTHERS_ORDER

    # Sanity: the order really does exist for its owner.
    owner_call = await _call(harness, "order_query", {"order_id": OTHERS_ORDER}, business_user_id="user_001")
    assert owner_call.json()["details"]["result"]["found"] is False

    direct, _ = harness["executor"], None
    token = current_user.set(UserContext(account_id=1, username="u", business_user_id="user_002"))
    try:
        as_owner = await direct.execute("order_query", {"order_id": OTHERS_ORDER, "user_id": "user_002"})
    finally:
        current_user.reset(token)
    assert as_owner.result["found"] is True, "the order belongs to user_002"


@pytest.mark.asyncio
async def test_write_authority_fields_are_stripped(harness):
    """`confirmed` / `approval_id` are never accepted from the caller (v2 §7)."""
    response = await _call(
        harness,
        "order_query",
        {"order_id": OWN_ORDER, "confirmed": True, "approval_id": "apr-1", "account_id": 999, "session_id": "other"},
    )
    body = response.json()
    assert sorted(body["details"]["audit"]["strippedFields"]) == ["account_id", "approval_id", "confirmed", "session_id"]
    assert body["details"]["requiresConfirmation"] is False


@pytest.mark.asyncio
async def test_only_read_tools_are_reachable(harness):
    """Write tools are unreachable, not merely filtered."""
    for tool in ("refund_create", "ticket_create", "refund_confirm", "bash"):
        response = await _call(harness, tool, {"order_id": OWN_ORDER})
        assert response.status_code == 403, tool
        assert response.json()["detail"]["code"] == "tool_not_allowed_on_internal_channel"


@pytest.mark.asyncio
async def test_unknown_arguments_become_a_failed_result_not_a_crash(harness):
    """P2-3, Python side.

    The tool contract rejects the call (`TypeError` inside the handler) and the
    existing ToolExecutor converts that into a failed result — unchanged
    behaviour. The primary schema rejection happens in the TS tool shell before
    the HTTP call; this test pins what the runtime does if one ever gets through.
    """
    response = await _call(harness, "order_query", {"order_id": OWN_ORDER, "bogus_field": "x"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["details"]["success"] is False
    assert body["details"]["status"] == "failed"
    assert body["details"]["errorCode"] == "execution_error"
    assert "bogus_field" in (body["details"]["error"] or "")
    # The failure text is what the model will read.
    assert "bogus_field" in body["content"]


@pytest.mark.asyncio
async def test_service_token_must_carry_business_user_id(harness):
    """D5: tool calls require the resolved identity."""
    response = await _call(harness, "order_query", {"order_id": OWN_ORDER}, business_user_id=None)
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_service_token_session_must_match_body(harness):
    headers = service_header(
        account_id=harness["account_id"],
        session_id="some-other-session",
        business_user_id=harness["business_user_id"],
    )
    response = await harness["http"].post(
        "/internal/tools/execute",
        json={
            "tool": "order_query",
            "arguments": {"order_id": OWN_ORDER},
            "session_id": harness["session_id"],
            "client_request_id": "req-1",
        },
        headers=headers,
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_session_must_belong_to_the_token_account(harness, tmp_path):
    stranger = seed_account(random_username("stranger"), "user_003")
    headers = service_header(
        account_id=stranger, session_id=harness["session_id"], business_user_id="user_003"
    )
    response = await harness["http"].post(
        "/internal/tools/execute",
        json={
            "tool": "order_query",
            "arguments": {"order_id": OWN_ORDER},
            "session_id": harness["session_id"],
            "client_request_id": "req-1",
        },
        headers=headers,
    )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_missing_service_token_is_401(harness):
    response = await harness["http"].post(
        "/internal/tools/execute",
        json={
            "tool": "order_query",
            "arguments": {"order_id": OWN_ORDER},
            "session_id": harness["session_id"],
            "client_request_id": "req-1",
        },
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_risk_check_identity_is_also_force_bound(harness):
    """risk_check is outside `customer_tool_arguments`' set, so the channel binds it."""
    response = await _call(harness, "risk_check", {"action": "refund", "amount": 60000, "user_id": "user_999"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["details"]["audit"]["strippedFields"] == ["user_id"]
    assert body["details"]["result"]["user_id"] == harness["business_user_id"]
    assert body["details"]["result"]["risk_level"] == "high"
    assert body["details"]["result"]["requires_manual_review"] is True


def _effective_arguments(executor, tool_name: str, arguments: dict, business_user_id: str) -> dict:
    """Reproduce the channel's strip + force-bind so the comparison is apples-to-apples."""
    from internal_api.tools import IDENTITY_FIELDS

    effective = {key: value for key, value in arguments.items() if key not in IDENTITY_FIELDS}
    schema = executor.server.get_tool(tool_name).input_schema
    if "user_id" in (schema.get("properties") or {}):
        effective["user_id"] = business_user_id
    return effective


@pytest.mark.asyncio
async def test_parity_with_direct_tool_executor(harness):
    """P2-7: the channel adds no business semantics of its own.

    For every READ tool the channel's outcome (success / status / error code)
    and structured payload must equal a direct ToolExecutor call made with the
    same effective arguments.
    """
    cases = [
        ("order_query", {"order_id": OWN_ORDER}),
        ("order_query", {"order_id": "ORD-20260801-9999"}),
        ("order_query", {"order_id": OTHERS_ORDER, "user_id": "user_002"}),
        ("ticket_query", {"ticket_id": "TK-1"}),
        ("refund_evaluate", {"order_id": OWN_ORDER}),
        ("refund_evaluate", {"order_id": "ORD-20260801-9999"}),
        ("risk_check", {"action": "refund", "amount": 12000}),
        ("risk_check", {"action": "payout", "amount": 90000}),
    ]
    for index, (tool, arguments) in enumerate(cases):
        response = await _call(harness, tool, arguments, client_request_id=f"req-parity-{index}")
        assert response.status_code == 200, (tool, response.text)
        channel = response.json()

        effective = _effective_arguments(harness["executor"], tool, arguments, harness["business_user_id"])
        token = current_user.set(
            UserContext(account_id=harness["account_id"], username="u", business_user_id=harness["business_user_id"])
        )
        try:
            direct = await harness["executor"].execute(tool, effective, ToolExecutionContext())
        finally:
            current_user.reset(token)

        assert channel["details"]["success"] == direct.success, tool
        assert channel["details"]["errorCode"] == direct.error_code, tool
        assert channel["details"]["status"] == direct.status, tool
        # The structured payload is passed through byte-for-byte.
        assert channel["details"]["result"] == direct.result, tool
        # And the executor block reports the same attempt count (no extra retry
        # layer was added on the channel side).
        assert channel["executor"]["retries"] == max(0, direct.attempts - 1), tool


@pytest.mark.asyncio
async def test_oversized_results_are_truncated_and_flagged(harness, monkeypatch):
    """P2-6: bounded content, marker, and the full payload still in details."""
    monkeypatch.setenv("SMARTCS_TOOL_RESULT_MAX_CHARS", "200")
    response = await _call(harness, "order_query", {"order_id": OWN_ORDER})
    body = response.json()
    assert body["details"]["contentTruncated"] is True
    assert body["content"].endswith("…<truncated>")
    assert len(body["content"]) <= 200
    # details keeps the untruncated structure so the harness/eval can inspect it.
    assert body["details"]["result"]["found"] is True


@pytest.mark.asyncio
async def test_knowledge_search_runs_through_the_shared_retriever(harness):
    """The channel reuses the process-wide retriever instance (no second index)."""
    response = await _call(harness, "knowledge_search", {"query": "退款政策", "top_k": 2})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["details"]["success"] is True
    assert isinstance(body["details"]["result"], list)
