"""Phase 5b acceptance: the refund two-phase flow, live, against a temp SQLite.

Covers P5-1 (full chain), P5-2 (no confirmation), P5-6 (expiry), P5-7
(cross-user / cross-session). Every write lands in `tmp_path/orders.db`.
"""

from __future__ import annotations

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

ORDER_ID = "ORD-20260801-0002"  # belongs to user_002


@pytest.fixture(autouse=True)
def _environment(monkeypatch):
    monkeypatch.setenv("INTERNAL_SERVICE_JWT_SECRET", SERVICE_SECRET)
    monkeypatch.setenv("AUTH_JWT_SECRET", USER_SECRET)
    monkeypatch.setenv("MYSQL_DATABASE", TEST_DATABASE)
    monkeypatch.setenv("SMARTCS_WRITE_MODE", "live")


@pytest_asyncio.fixture
async def live(tmp_path):
    from mcp.execution_ledger import ExecutionLedger
    from mcp.mcp_server import MCPToolServer, create_default_tools
    from mcp.order_repository import OrderRepository
    from mcp.tool_execution import ToolExecutor
    from platform_db.database import PlatformDatabase
    from refunds.service import RefundService
    from tickets.service import TicketService

    apply_migration()
    repository = OrderRepository(str(tmp_path / "orders.db"))
    server = create_default_tools(
        MCPToolServer(),
        order_repository=repository,
        refund_service=RefundService(repository),
        ticket_service=TicketService(repository),
    )
    executor = ToolExecutor(server, ledger=ExecutionLedger(repository.db_path))
    app = await build_app(tool_executor=executor)
    app.state.platform_database = PlatformDatabase.from_env()

    account_id = seed_account(random_username("r5b"), "user_002")
    seed_session("sess-refund", account_id, "pi")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        yield {
            "http": http,
            "repository": repository,
            "app": app,
            "account_id": account_id,
            "session_id": "sess-refund",
            "business_user_id": "user_002",
        }


async def _seed_receipt(session_id: str, client_request_id: str) -> None:
    from platform_db.database import PlatformDatabase

    db = PlatformDatabase.from_env()

    async def _run():
        def write(_c, cur):
            cur.execute(
                """INSERT INTO agent_run_receipt
                     (session_id, client_request_id, request_hash, status, open_write_operations)
                   VALUES (%s,%s,'h','processing',JSON_ARRAY())
                   ON DUPLICATE KEY UPDATE id = id""",
                (session_id, client_request_id),
            )
            return True

        await db._call(write)

    await _run()


def _post(live, tool, arguments, client_request_id, **kw):
    # Read each override once: the token and the body must agree, and popping
    # the same key twice would silently give the header a different value.
    session_id = kw.get("session_id", live["session_id"])
    account_id = kw.get("account_id", live["account_id"])
    business_user_id = kw.get("business_user_id", live["business_user_id"])
    return live["http"].post(
        "/internal/tools/execute",
        json={
            "tool": tool,
            "arguments": arguments,
            "session_id": session_id,
            "client_request_id": client_request_id,
        },
        headers=service_header(
            account_id=account_id,
            session_id=session_id,
            business_user_id=business_user_id,
            client_request_id=client_request_id,
        ),
    )


def _refund_count(live) -> int:
    with live["repository"].transaction() as connection:
        return int(connection.execute("SELECT COUNT(*) FROM refunds").fetchone()[0])


async def _pending_row(pending_id: str):
    from platform_db.database import PlatformDatabase

    db = PlatformDatabase.from_env()

    def read(_c, cur):
        cur.execute("SELECT * FROM pending_action WHERE id=%s", (pending_id,))
        return cur.fetchone()

    return await db._call(read)


@pytest.mark.asyncio
async def test_p5_1_full_two_phase_chain_writes_exactly_one_refund(live):
    """P5-1: evaluate opens a real pending_action; the confirm turn refunds once."""
    seed_memory_source_event("sess-refund", "user_002", "req-eval", f"我要退款，订单 {ORDER_ID}")
    await _seed_receipt("sess-refund", "req-eval")
    before = _refund_count(live)

    evaluated = await _post(live, "refund_evaluate", {"order_id": ORDER_ID}, "req-eval")
    assert evaluated.status_code == 200, evaluated.text
    pending_id = evaluated.json()["details"].get("pending_action_id")
    assert pending_id, "live refund_evaluate must open a real pending_action"

    row = await _pending_row(pending_id)
    assert row is not None
    assert row["status"] == "pending"
    assert row["operation_id"] is None
    assert str(row["session_id"]) == "sess-refund"
    assert row["expires_at"] is not None  # TTL is real, not a placeholder

    # The confirmation turn.
    seed_memory_source_event("sess-refund", "user_002", "req-confirm", "确认退款")
    await _seed_receipt("sess-refund", "req-confirm")
    confirmed = await _post(live, "refund_confirm", {"pending_action_id": pending_id}, "req-confirm")
    body = confirmed.json()

    assert body["details"]["authorized"] is True, body["details"]
    assert body["details"]["executed"] is True, body["details"]
    assert _refund_count(live) == before + 1

    after = await _pending_row(pending_id)
    assert after["status"] == "consumed"
    assert after["operation_id"] == body["details"]["operationId"]


@pytest.mark.asyncio
async def test_p5_2_without_confirmation_nothing_is_written(live):
    """P5-2: evaluating is not confirming; the pending action stays pending."""
    seed_memory_source_event("sess-refund", "user_002", "req-e2", f"我要退款，订单 {ORDER_ID}")
    await _seed_receipt("sess-refund", "req-e2")
    before = _refund_count(live)

    evaluated = await _post(live, "refund_evaluate", {"order_id": ORDER_ID}, "req-e2")
    pending_id = evaluated.json()["details"]["pending_action_id"]

    # A non-confirming turn that still names the pending action.
    seed_memory_source_event("sess-refund", "user_002", "req-nc", "我再想想，先不退了")
    await _seed_receipt("sess-refund", "req-nc")
    refused = await _post(live, "refund_confirm", {"pending_action_id": pending_id}, "req-nc")
    body = refused.json()

    assert body["details"]["authorized"] is False
    assert body["details"]["executed"] is False
    assert body["details"]["errorCode"] == "explicit_confirmation_required"
    assert _refund_count(live) == before

    row = await _pending_row(pending_id)
    assert row["status"] == "pending"  # untouched, still confirmable


@pytest.mark.asyncio
async def test_p5_6_expired_pending_action_is_refused_without_writing(live):
    """P5-6: an expired pending action cannot be consumed."""
    from platform_db.database import PlatformDatabase

    seed_memory_source_event("sess-refund", "user_002", "req-exp", f"我要退款，订单 {ORDER_ID}")
    await _seed_receipt("sess-refund", "req-exp")
    before = _refund_count(live)

    evaluated = await _post(live, "refund_evaluate", {"order_id": ORDER_ID}, "req-exp")
    pending_id = evaluated.json()["details"]["pending_action_id"]

    # Age the pending action past its TTL.
    db = PlatformDatabase.from_env()
    await db._call(
        lambda _c, cur: cur.execute(
            "UPDATE pending_action SET expires_at = DATE_SUB(CURRENT_TIMESTAMP(3), INTERVAL 1 MINUTE) WHERE id=%s",
            (pending_id,),
        )
    )

    seed_memory_source_event("sess-refund", "user_002", "req-exp2", "确认退款")
    await _seed_receipt("sess-refund", "req-exp2")
    refused = await _post(live, "refund_confirm", {"pending_action_id": pending_id}, "req-exp2")
    body = refused.json()

    assert body["details"]["authorized"] is False
    assert body["details"]["errorCode"] == "pending_action_expired"
    assert _refund_count(live) == before
    assert (await _pending_row(pending_id))["status"] == "expired"


@pytest.mark.asyncio
async def test_p5_7_cross_user_pending_action_fails_closed(live, tmp_path):
    """P5-7: a pending action belonging to someone else can never be consumed."""
    seed_memory_source_event("sess-refund", "user_002", "req-own", f"我要退款，订单 {ORDER_ID}")
    await _seed_receipt("sess-refund", "req-own")
    evaluated = await _post(live, "refund_evaluate", {"order_id": ORDER_ID}, "req-own")
    pending_id = evaluated.json()["details"]["pending_action_id"]

    # A second account, with its own session, tries to confirm it.
    other = seed_account(random_username("intruder"), "user_003")
    seed_session("sess-intruder", other, "pi")
    await _seed_receipt("sess-intruder", "req-steal")
    seed_memory_source_event("sess-intruder", "user_003", "req-steal", "确认退款")

    before = _refund_count(live)
    stolen = await _post(
        live,
        "refund_confirm",
        {"pending_action_id": pending_id},
        "req-steal",
        account_id=other,
        session_id="sess-intruder",
        business_user_id="user_003",
    )
    body = stolen.json()

    assert body["details"]["authorized"] is False
    assert body["details"]["executed"] is False
    assert body["details"]["errorCode"] == "pending_action_session_mismatch"
    assert _refund_count(live) == before
    assert (await _pending_row(pending_id))["status"] == "pending"
