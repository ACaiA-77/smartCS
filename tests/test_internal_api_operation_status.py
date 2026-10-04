"""Phase 5c acceptance: the recovery authority.

P5-3 (idempotent replay), P5-4 (timeout -> UNKNOWN, reconcile, no blind retry),
P5-5 (write completed but the transcript lost the toolResult).
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from internal_api.operation_status import verdict_from_ledger
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

ORDER_ID = "ORD-20260801-0002"


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
    app.state.execution_ledger = ledger

    account_id = seed_account(random_username("r5c"), "user_002")
    seed_session("sess-ops", account_id, "pi")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        yield {
            "http": http,
            "repository": repository,
            "ledger": ledger,
            "app": app,
            "account_id": account_id,
            "session_id": "sess-ops",
            "business_user_id": "user_002",
        }


async def _seed_receipt(session_id: str, client_request_id: str) -> None:
    from platform_db.database import PlatformDatabase

    db = PlatformDatabase.from_env()

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


def _post(live, tool, arguments, client_request_id):
    return live["http"].post(
        "/internal/tools/execute",
        json={
            "tool": tool,
            "arguments": arguments,
            "session_id": live["session_id"],
            "client_request_id": client_request_id,
        },
        headers=service_header(
            account_id=live["account_id"],
            session_id=live["session_id"],
            business_user_id=live["business_user_id"],
            client_request_id=client_request_id,
        ),
    )


async def _status(live, operation_id, client_request_id="req-status"):
    return await live["http"].post(
        "/internal/operation_status",
        json={
            "session_id": live["session_id"],
            "client_request_id": client_request_id,
            "operation_id": operation_id,
        },
        headers=service_header(
            account_id=live["account_id"],
            session_id=live["session_id"],
            business_user_id=live["business_user_id"],
            client_request_id=client_request_id,
        ),
    )


def _refund_count(live) -> int:
    with live["repository"].transaction() as connection:
        return int(connection.execute("SELECT COUNT(*) FROM refunds").fetchone()[0])


# --- the verdict mapping is the whole safety argument, so test it directly ---

def test_verdict_mapping_is_fail_safe():
    assert verdict_from_ledger(None)[0] == "PROVABLY_NOT_EXECUTED"
    assert verdict_from_ledger({"status": "completed"})[0] == "COMPLETED"
    assert verdict_from_ledger({"status": "failed"})[0] == "FAILED"
    # A dangling claim must never be read as "safe to retry".
    assert verdict_from_ledger({"status": "in_progress"})[0] == "UNKNOWN"
    assert verdict_from_ledger({"status": "something-new"})[0] == "UNKNOWN"


async def _evaluate_and_confirm(live, client_request_id: str) -> tuple[str, dict]:
    seed_memory_source_event("sess-ops", "user_002", f"{client_request_id}-eval", f"我要退款，订单 {ORDER_ID}")
    await _seed_receipt("sess-ops", f"{client_request_id}-eval")
    evaluated = await _post(live, "refund_evaluate", {"order_id": ORDER_ID}, f"{client_request_id}-eval")
    pending_id = evaluated.json()["details"]["pending_action_id"]

    seed_memory_source_event("sess-ops", "user_002", client_request_id, "确认退款")
    await _seed_receipt("sess-ops", client_request_id)
    confirmed = await _post(live, "refund_confirm", {"pending_action_id": pending_id}, client_request_id)
    return pending_id, confirmed.json()


@pytest.mark.asyncio
async def test_p5_3_replaying_a_confirmed_refund_does_not_refund_twice(live):
    """P5-3: the same request re-sent must not produce a second refund."""
    _, first = await _evaluate_and_confirm(live, "req-p53")
    assert first["details"]["executed"] is True
    after_first = _refund_count(live)

    # Replay through the *operation* path: the ledger recognises the operation
    # id and replays instead of executing again.
    replayed = await _post(
        live,
        "refund_confirm",
        {"pending_action_id": first["details"].get("pendingActionId") or "ignored"},
        "req-p53",
    )
    assert replayed.status_code == 200
    assert _refund_count(live) == after_first, "replay must not create a second refund"


@pytest.mark.asyncio
async def test_p5_4_unknown_then_reconcile_never_blind_retries(live):
    """P5-4: an interrupted write is UNKNOWN until the ledger says otherwise."""
    _, confirmed = await _evaluate_and_confirm(live, "req-p54")
    operation_id = confirmed["details"]["operationId"]
    after = _refund_count(live)

    # The write DID land, so the ledger holds the authoritative answer.
    completed = await _status(live, operation_id)
    body = completed.json()
    assert body["status"] == "COMPLETED"
    assert body["ledgerStatus"] == "completed"

    # A dangling claim (crashed mid-write) is UNKNOWN, not "safe to retry".
    live["ledger"].claim("op-unknown-1", "refund_create", "hash-x")
    unknown = (await _status(live, "op-unknown-1")).json()
    assert unknown["status"] == "UNKNOWN"
    assert "in_progress" in unknown["detail"]

    # An operation that never existed is the ONLY provably-safe case.
    absent = (await _status(live, "op-never-sent")).json()
    assert absent["status"] == "PROVABLY_NOT_EXECUTED"

    # Reconciling must not itself write anything.
    assert _refund_count(live) == after


@pytest.mark.asyncio
async def test_p5_5_completed_write_can_be_recovered_without_replay(live):
    """P5-5: ledger COMPLETED + lost toolResult → recover from the authority."""
    _, confirmed = await _evaluate_and_confirm(live, "req-p55")
    operation_id = confirmed["details"]["operationId"]
    after = _refund_count(live)

    # Simulate the F5 shape: the harness never appended the toolResult, so it
    # asks the authority instead of re-running the tool.
    recovered = (await _status(live, operation_id)).json()
    assert recovered["status"] == "COMPLETED"
    assert recovered["result"] is not None, "recovery needs the stored result"

    # Finishing the request from the ledger must not touch the domain again.
    assert _refund_count(live) == after
