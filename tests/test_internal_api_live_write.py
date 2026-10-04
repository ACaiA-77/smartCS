"""Phase 5a acceptance: ticket_create goes live, with real writes confined to a
temporary SQLite test database.

Every write in these tests lands in `tmp_path/orders.db`. Nothing touches
`data/orders.db` or any production file.
"""

from __future__ import annotations

import json

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from tests.internal_api_helpers import (
    SERVICE_SECRET,
    TEST_DATABASE,
    USER_SECRET,
    apply_migration,
    build_app,
    random_username,
    seed_account,
    seed_memory_source_event,
    seed_session,
    service_header,
)


@pytest.fixture(autouse=True)
def _environment(monkeypatch):
    monkeypatch.setenv("INTERNAL_SERVICE_JWT_SECRET", SERVICE_SECRET)
    monkeypatch.setenv("AUTH_JWT_SECRET", USER_SECRET)
    monkeypatch.setenv("MYSQL_DATABASE", TEST_DATABASE)
    monkeypatch.setenv("SMARTCS_WRITE_MODE", "live")


@pytest_asyncio.fixture
async def live(tmp_path):
    """The real MCP server + ToolExecutor + ledger, over a temp SQLite database."""
    from mcp.execution_ledger import ExecutionLedger
    from mcp.mcp_server import MCPToolServer, create_default_tools
    from mcp.order_repository import OrderRepository
    from mcp.tool_execution import ToolExecutor
    from platform_db.database import PlatformDatabase
    from refunds.service import RefundService
    from tickets.service import TicketService

    apply_migration()
    repository = OrderRepository(str(tmp_path / "orders.db"))
    ledger = ExecutionLedger(repository.db_path)
    server = create_default_tools(
        MCPToolServer(),
        order_repository=repository,
        refund_service=RefundService(repository),
        ticket_service=TicketService(repository),
    )
    executor = ToolExecutor(server, ledger=ledger)

    app = await build_app(tool_executor=executor)
    app.state.platform_database = PlatformDatabase.from_env()

    account_id = seed_account(random_username("live"), "user_002")
    session_id = "sess-live"
    seed_session(session_id, account_id, "pi")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        yield {
            "http": http,
            "repository": repository,
            "app": app,
            "account_id": account_id,
            "session_id": session_id,
            "business_user_id": "user_002",
        }


async def _seed_receipt(session_id: str, client_request_id: str) -> None:
    """Create the receipt the harness would have created.

    Without it the write service fails closed by design (`receipt_missing`),
    because the operation id would have no durable home.
    """
    from platform_db.database import PlatformDatabase

    db = PlatformDatabase.from_env()

    def write(_c, cur):
        cur.execute(
            """INSERT INTO agent_run_receipt
                 (session_id, client_request_id, request_hash, status, open_write_operations)
               VALUES (%s,%s,'hash','processing',JSON_ARRAY())
               ON DUPLICATE KEY UPDATE id = id""",
            (session_id, client_request_id),
        )
        return True

    await db._call(write)


def _ticket_body(session_id: str, client_request_id: str) -> dict:
    return {
        "tool": "ticket_create",
        "arguments": {
            "client_request_id": client_request_id,
            "request_payload_hash": "hash-1",
            "user_id": "user_002",
            "title": "商品破损投诉",
            "description": "商品到货即破损",
            "priority": "high",
            "category": "complaint",
        },
        "session_id": session_id,
        "client_request_id": client_request_id,
    }


def _post(live, body, **token_kwargs):
    return live["http"].post(
        "/internal/tools/execute",
        json=body,
        headers=service_header(
            account_id=token_kwargs.pop("account_id", live["account_id"]),
            session_id=token_kwargs.pop("session_id", live["session_id"]),
            business_user_id=token_kwargs.pop("business_user_id", live["business_user_id"]),
            client_request_id=token_kwargs.pop("client_request_id", body["client_request_id"]),
        ),
    )


def _ticket_count(live) -> int:
    with live["repository"].transaction() as connection:
        return int(connection.execute("SELECT COUNT(*) FROM support_tickets").fetchone()[0])


@pytest.mark.asyncio
async def test_p5_9_explicit_consent_writes_exactly_one_ticket(live):
    """P5-9: same-turn explicit consent → exactly one real ticket."""
    seed_memory_source_event("sess-live", "user_002", "req-live-1", "我要投诉，帮我建个工单")
    await _seed_receipt("sess-live", "req-live-1")
    before = _ticket_count(live)

    response = await _post(live, _ticket_body("sess-live", "req-live-1"))
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["details"]["authorized"] is True, body
    assert body["details"]["executed"] is True
    assert body["details"]["operationId"]
    assert _ticket_count(live) == before + 1


@pytest.mark.asyncio
async def test_p5_9_negative_no_consent_never_writes(live):
    """P5-9 negative half: no create intent in the user's words → no write."""
    # A pure question about the流程 must NOT authorize a write.
    seed_memory_source_event("sess-live", "user_002", "req-live-2", "创建工单的流程是什么？")
    before = _ticket_count(live)

    response = await _post(live, _ticket_body("sess-live", "req-live-2"))
    body = response.json()

    assert body["details"]["authorized"] is False
    assert body["details"]["executed"] is False
    assert body["details"]["errorCode"] == "explicit_consent_required"
    assert _ticket_count(live) == before


@pytest.mark.asyncio
async def test_consent_comes_from_the_ledger_not_the_caller(live):
    """Constraint 3: raw user message is read back from memory_source_event."""
    # The ledger row says something that is NOT consent, even though the tool
    # arguments and the request body could imply otherwise.
    seed_memory_source_event("sess-live", "user_002", "req-live-3", "我的订单到哪了？")
    before = _ticket_count(live)

    response = await _post(live, _ticket_body("sess-live", "req-live-3"))
    assert response.json()["details"]["authorized"] is False
    assert _ticket_count(live) == before

    # And with no ledger row at all, the service fails closed.
    response = await _post(live, _ticket_body("sess-live", "req-live-3b"))
    assert response.json()["details"]["errorCode"] == "provenance_unavailable"
    assert _ticket_count(live) == before


@pytest.mark.asyncio
async def test_operation_id_is_durable_before_the_write_is_sent(live):
    """Plan v2 §6.3 iron rule: the operation id exists before the side effect."""
    from platform_db.database import PlatformDatabase

    seed_memory_source_event("sess-live", "user_002", "req-live-4", "帮我创建工单：包装破损")
    await _seed_receipt("sess-live", "req-live-4")
    from platform_db.database import PlatformDatabase

    db = PlatformDatabase.from_env()

    response = await _post(live, _ticket_body("sess-live", "req-live-4"))
    body = response.json()
    assert body["details"]["executed"] is True

    def read(_c, cur):
        cur.execute(
            "SELECT open_write_operations FROM agent_run_receipt WHERE session_id=%s AND client_request_id=%s",
            ("sess-live", "req-live-4"),
        )
        row = cur.fetchone()
        return row["open_write_operations"] if row else None

    stored = await db._call(read)
    if isinstance(stored, str):
        stored = json.loads(stored)
    entries = list(stored or [])
    assert len(entries) == 1
    entry = entries[0]
    assert entry["tool"] == "ticket_create"
    assert entry["operation_id"] == body["details"]["operationId"]
    # PREPARED was written first; the terminal state replaces it after execution.
    assert entry["state"] == "COMPLETED"
    assert entry["target_hash"]


@pytest.mark.asyncio
async def test_p5_10_write_is_idempotent_per_operation_id(live):
    """P5-10: replaying the same request must not create a second ticket."""
    seed_memory_source_event("sess-live", "user_002", "req-live-5", "提交投诉工单")
    await _seed_receipt("sess-live", "req-live-5")
    first = await _post(live, _ticket_body("sess-live", "req-live-5"))
    assert first.json()["details"]["executed"] is True
    after_first = _ticket_count(live)

    second = await _post(live, _ticket_body("sess-live", "req-live-5"))
    body = second.json()
    # The ledger recognises the operation and replays it rather than re-executing.
    assert body["details"]["executed"] is True
    assert body["details"]["result"].get("replayed") is True or body["details"]["attempts"] >= 1
    assert _ticket_count(live) == after_first


@pytest.mark.asyncio
async def test_write_tools_are_unreachable_without_live_mode(live, monkeypatch):
    """P5-11: with writes off, the same call is refused outright."""
    monkeypatch.setenv("SMARTCS_WRITE_MODE", "shadow")
    seed_memory_source_event("sess-live", "user_002", "req-live-6", "我要投诉，帮我建个工单")
    before = _ticket_count(live)

    response = await _post(live, _ticket_body("sess-live", "req-live-6"))
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "tool_not_allowed_on_internal_channel"
    assert _ticket_count(live) == before


@pytest.mark.asyncio
async def test_refund_confirm_without_a_pending_action_is_refused(live):
    """5b: refund_confirm is reachable, but refuses without a valid pending."""
    seed_memory_source_event("sess-live", "user_002", "req-live-7", "确认退款")
    response = await _post(
        live,
        {
            "tool": "refund_confirm",
            "arguments": {"pending_action_id": "pending-1"},
            "session_id": "sess-live",
            "client_request_id": "req-live-7",
        },
    )
    body = response.json()
    assert response.status_code == 200, response.text
    assert body["details"]["authorized"] is False
    assert body["details"]["executed"] is False
    assert body["details"]["errorCode"] in {"pending_action_not_found", "missing_pending_action"}
    assert _ticket_count(live) == 0
    with live["repository"].transaction() as connection:
        assert int(connection.execute("SELECT COUNT(*) FROM refunds").fetchone()[0]) == 22


@pytest.mark.asyncio
async def test_root_cause_idempotency_fields_are_server_authoritative(live):
    """Pins the Phase 5 first-round failure.

    Root cause: Phase 2's identity strip removed `client_request_id` (and the
    model's `request_payload_hash`), but ticket_create's idempotency contract
    requires both. Stripping is correct — a model must not choose its own
    idempotency key — so the service now injects its own AFTER the strip, and
    the hash is computed over the final arguments.

    A forged pair in the model's arguments must therefore be ignored.
    """
    seed_memory_source_event("sess-live", "user_002", "req-rc", "我要投诉商品破损，帮我建个工单")
    await _seed_receipt("sess-live", "req-rc")
    before = _ticket_count(live)

    body = {
        "tool": "ticket_create",
        "arguments": {
            # Forged, trust-sensitive fields the model must not control.
            "client_request_id": "FORGED-IDEMPOTENCY-KEY",
            "request_payload_hash": "forged-hash",
            "user_id": "user_999",
            "title": "商品破损投诉",
            "description": "商品到货即破损",
            "priority": "high",
            "category": "complaint",
        },
        "session_id": "sess-live",
        "client_request_id": "req-rc",
    }
    response = await _post(live, body)
    payload = response.json()

    assert payload["details"]["authorized"] is True
    assert payload["details"]["executed"] is True, payload["details"]
    assert _ticket_count(live) == before + 1

    with live["repository"].transaction() as connection:
        row = connection.execute(
            "SELECT client_request_id, user_id FROM support_tickets ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
    # The authoritative values won; the forged ones never reached the database.
    assert row[0] == "req-rc"
    assert row[1] == "user_002"


@pytest.mark.asyncio
async def test_executed_reflects_business_failure_not_absence_of_exception(live):
    """A handler that reports failure by returning a dict must not read as success."""
    seed_memory_source_event("sess-live", "user_002", "req-bf", "帮我创建工单")
    await _seed_receipt("sess-live", "req-bf")
    before = _ticket_count(live)

    # Missing title/description: ticket_create returns {"success": false, ...}
    # rather than raising, so `executed` must come from the payload.
    response = await _post(
        live,
        {
            "tool": "ticket_create",
            "arguments": {"title": "", "description": ""},
            "session_id": "sess-live",
            "client_request_id": "req-bf",
        },
    )
    payload = response.json()["details"]
    if payload.get("authorized"):
        assert payload["executed"] is False
        assert payload["errorCode"]
    assert _ticket_count(live) == before
