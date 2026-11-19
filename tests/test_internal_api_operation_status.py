"""Phase 5c acceptance: the recovery authority for two-phase Copilot actions.

P5-3 (idempotent replay), P5-4 (timeout -> UNKNOWN, reconcile, no blind retry),
P5-5 (write completed but the transcript lost the toolResult).
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from internal_api.operation_status import verdict_from_ledger
from platform_db.database import PlatformDatabase
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
from tool_runtime.approval_store import ApprovalService
from tool_runtime.execution_ledger import ExecutionLedger
from tool_runtime.mcp_server import MCPToolServer, create_default_tools
from tool_runtime.tool_execution import ToolExecutor


class _FakeMailProvider:
    def __init__(self, sent: list[dict]) -> None:
        self._sent = sent

    def send_message(self, **kwargs):
        self._sent.append(kwargs)
        return {"sent": True, "message_id": f"msg-{len(self._sent)}"}


class _FakeMailRouter:
    accounts = ["163"]

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self._provider = _FakeMailProvider(self.sent)

    def get_provider(self, account: str):
        assert account == "163"
        return self._provider


@pytest.fixture(autouse=True)
def _environment(monkeypatch):
    monkeypatch.setenv("INTERNAL_SERVICE_JWT_SECRET", SERVICE_SECRET)
    monkeypatch.setenv("AUTH_JWT_SECRET", USER_SECRET)
    monkeypatch.setenv("MYSQL_DATABASE", TEST_DATABASE)
    monkeypatch.setenv("SMARTCS_WRITE_MODE", "live")


@pytest_asyncio.fixture
async def live(tmp_path):
    apply_migration()
    mail_router = _FakeMailRouter()
    db_path = str(tmp_path / "copilot.db")
    ledger = ExecutionLedger(db_path)
    approvals = ApprovalService(db_path)
    server = create_default_tools(MCPToolServer(), mail_router=mail_router)
    executor = ToolExecutor(server, ledger=ledger, approval_service=approvals)
    app = await build_app(tool_executor=executor, execution_ledger=ledger)
    app.state.platform_database = PlatformDatabase.from_env()

    account_id = seed_account(random_username("r5c"), "user_002")
    seed_session("sess-ops", account_id, "pi")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        yield {
            "http": http,
            "mail_router": mail_router,
            "ledger": ledger,
            "app": app,
            "account_id": account_id,
            "session_id": "sess-ops",
            "business_user_id": "user_002",
        }


async def _seed_receipt(session_id: str, client_request_id: str) -> None:
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


def test_verdict_mapping_is_fail_safe():
    assert verdict_from_ledger(None)[0] == "PROVABLY_NOT_EXECUTED"
    assert verdict_from_ledger({"status": "completed"})[0] == "COMPLETED"
    assert verdict_from_ledger({"status": "failed"})[0] == "FAILED"
    assert verdict_from_ledger({"status": "in_progress"})[0] == "UNKNOWN"
    assert verdict_from_ledger({"status": "something-new"})[0] == "UNKNOWN"


async def _prepare_and_confirm(live, client_request_id: str) -> tuple[str, dict]:
    seed_memory_source_event(
        "sess-ops", "user_002", f"{client_request_id}-prep", "帮我给 alice 发一封项目周报邮件"
    )
    await _seed_receipt("sess-ops", f"{client_request_id}-prep")
    prepared = await _post(
        live,
        "copilot_action_prepare",
        {
            "tool": "mail_send",
            "arguments": {
                "account": "163",
                "to": "alice@example.com",
                "subject": "Weekly Sync",
                "body": "Status update",
            },
        },
        f"{client_request_id}-prep",
    )
    pending_id = prepared.json()["details"]["pending_action_id"]
    assert pending_id

    seed_memory_source_event("sess-ops", "user_002", client_request_id, "确认发送")
    await _seed_receipt("sess-ops", client_request_id)
    confirmed = await _post(
        live, "copilot_action_confirm", {"pending_action_id": pending_id}, client_request_id
    )
    return pending_id, confirmed.json()


@pytest.mark.asyncio
async def test_p5_3_replaying_a_confirmed_action_does_not_send_twice(live):
    """P5-3: the same request re-sent must not send a second email."""
    _, first = await _prepare_and_confirm(live, "req-p53")
    assert first["details"]["executed"] is True
    after_first = len(live["mail_router"].sent)
    assert after_first == 1

    replayed = await _post(
        live,
        "copilot_action_confirm",
        {"pending_action_id": first["details"].get("pendingActionId") or "ignored"},
        "req-p53",
    )
    assert replayed.status_code == 200
    assert len(live["mail_router"].sent) == after_first, "replay must not send a second email"


@pytest.mark.asyncio
async def test_p5_4_unknown_then_reconcile_never_blind_retries(live):
    """P5-4: an interrupted write is UNKNOWN until the ledger says otherwise."""
    _, confirmed = await _prepare_and_confirm(live, "req-p54")
    operation_id = confirmed["details"]["operationId"]
    after = len(live["mail_router"].sent)

    completed = await _status(live, operation_id)
    body = completed.json()
    assert body["status"] == "COMPLETED"
    assert body["ledgerStatus"] == "completed"

    live["ledger"].claim("op-unknown-1", "mail_send", "hash-x")
    unknown = (await _status(live, "op-unknown-1")).json()
    assert unknown["status"] == "UNKNOWN"
    assert "in_progress" in unknown["detail"]

    absent = (await _status(live, "op-never-sent")).json()
    assert absent["status"] == "PROVABLY_NOT_EXECUTED"

    assert len(live["mail_router"].sent) == after


@pytest.mark.asyncio
async def test_p5_5_completed_write_can_be_recovered_without_replay(live):
    """P5-5: ledger COMPLETED + lost toolResult → recover from the authority."""
    _, confirmed = await _prepare_and_confirm(live, "req-p55")
    operation_id = confirmed["details"]["operationId"]
    after = len(live["mail_router"].sent)

    recovered = (await _status(live, operation_id)).json()
    assert recovered["status"] == "COMPLETED"
    assert recovered["result"] is not None

    assert len(live["mail_router"].sent) == after
