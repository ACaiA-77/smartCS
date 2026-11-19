"""Phase 5a acceptance: Copilot local writes go live with real writes confined to
a temporary Obsidian vault and SQLite ledger.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from internal_api.write_authorization import AuthorizationDenied, WriteAuthorizationService
from knowledge_engine import ObsidianVault, OntologyStore, WikiCompiler
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
from tool_runtime.execution_ledger import ExecutionLedger
from tool_runtime.mcp_server import MCPToolServer, create_default_tools
from tool_runtime.tool_execution import ToolExecutor


@pytest.fixture(autouse=True)
def _environment(monkeypatch):
    monkeypatch.setenv("INTERNAL_SERVICE_JWT_SECRET", SERVICE_SECRET)
    monkeypatch.setenv("AUTH_JWT_SECRET", USER_SECRET)
    monkeypatch.setenv("MYSQL_DATABASE", TEST_DATABASE)
    monkeypatch.setenv("SMARTCS_WRITE_MODE", "live")


@pytest_asyncio.fixture
async def live(tmp_path):
    """The real MCP server + ToolExecutor + ledger, over temp Vault & SQLite."""
    apply_migration()
    vault = ObsidianVault(tmp_path / "vault")
    ontology = OntologyStore(tmp_path / "ontology.sqlite")
    wiki_compiler = WikiCompiler(vault, ontology)
    ledger = ExecutionLedger(str(tmp_path / "copilot.db"))
    server = create_default_tools(
        MCPToolServer(),
        vault=vault,
        ontology=ontology,
        wiki_compiler=wiki_compiler,
    )
    executor = ToolExecutor(server, ledger=ledger)

    app = await build_app(tool_executor=executor, execution_ledger=ledger)
    app.state.platform_database = PlatformDatabase.from_env()

    account_id = seed_account(random_username("live"), "user_002")
    session_id = "sess-live"
    seed_session(session_id, account_id, "pi")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        yield {
            "http": http,
            "vault": vault,
            "ledger": ledger,
            "app": app,
            "account_id": account_id,
            "session_id": session_id,
            "business_user_id": "user_002",
        }
    ontology.close()


async def _seed_receipt(session_id: str, client_request_id: str) -> None:
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


def _daily_body(session_id: str, client_request_id: str, *, entry: str = "完成了代码审阅") -> dict:
    return {
        "tool": "vault_append_daily",
        "arguments": {
            "entry": entry,
            "date_str": "2026-11-18",
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


def _daily_line_count(live, marker: str = "完成了代码审阅") -> int:
    path = live["vault"].root / "daily" / "2026-11-18.md"
    if not path.exists():
        return 0
    return path.read_text(encoding="utf-8").count(marker)


@pytest.mark.asyncio
async def test_p5_9_explicit_consent_writes_exactly_once(live):
    """P5-9: same-turn explicit consent → exactly one local daily entry."""
    seed_memory_source_event("sess-live", "user_002", "req-live-1", "把今天的进度记到日记里")
    await _seed_receipt("sess-live", "req-live-1")
    before = _daily_line_count(live)

    response = await _post(live, _daily_body("sess-live", "req-live-1"))
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["details"]["authorized"] is True, body
    assert body["details"]["executed"] is True
    assert body["details"]["operationId"]
    assert _daily_line_count(live) == before + 1


@pytest.mark.asyncio
async def test_p5_9_negative_no_consent_never_writes(live):
    """P5-9 negative half: consultation question must NOT authorize a write."""
    seed_memory_source_event("sess-live", "user_002", "req-live-2", "向日记里写入内容的流程是什么？")
    before = _daily_line_count(live)

    response = await _post(live, _daily_body("sess-live", "req-live-2"))
    body = response.json()

    assert body["details"]["authorized"] is False
    assert body["details"]["executed"] is False
    assert body["details"]["errorCode"] == "explicit_consent_required"
    assert _daily_line_count(live) == before


@pytest.mark.asyncio
async def test_consent_comes_from_the_ledger_not_the_caller(live):
    """Constraint 3: raw user message is read back from memory_source_event."""
    seed_memory_source_event("sess-live", "user_002", "req-live-3", "帮我搜一下项目笔记")
    before = _daily_line_count(live)

    response = await _post(live, _daily_body("sess-live", "req-live-3"))
    assert response.json()["details"]["authorized"] is False
    assert _daily_line_count(live) == before

    # And with no ledger row at all, the service fails closed.
    response = await _post(live, _daily_body("sess-live", "req-live-3b"))
    assert response.json()["details"]["errorCode"] == "provenance_unavailable"
    assert _daily_line_count(live) == before


@pytest.mark.asyncio
async def test_operation_id_is_durable_before_the_write_is_sent(live):
    """Plan v2 §6.3 iron rule: the operation id exists before the side effect."""
    seed_memory_source_event("sess-live", "user_002", "req-live-4", "追加一条日记记录：架构收敛")
    await _seed_receipt("sess-live", "req-live-4")
    db = PlatformDatabase.from_env()

    response = await _post(live, _daily_body("sess-live", "req-live-4"))
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
    assert entry["tool"] == "vault_append_daily"
    assert entry["operation_id"] == body["details"]["operationId"]
    assert entry["state"] == "COMPLETED"
    assert entry["target_hash"]


@pytest.mark.asyncio
async def test_p5_10_write_is_idempotent_per_operation_id(live):
    """P5-10: replaying the same request must not duplicate the daily entry."""
    seed_memory_source_event("sess-live", "user_002", "req-live-5", "记下今天完成了验收")
    await _seed_receipt("sess-live", "req-live-5")
    first = await _post(live, _daily_body("sess-live", "req-live-5"))
    assert first.json()["details"]["executed"] is True
    after_first = _daily_line_count(live)

    second = await _post(live, _daily_body("sess-live", "req-live-5"))
    body = second.json()
    assert body["details"]["executed"] is True
    assert body["details"]["operationId"] == first.json()["details"]["operationId"]
    assert body["details"]["attempts"] == 0
    assert body["details"]["result"] == first.json()["details"]["result"]
    assert _daily_line_count(live) == after_first


@pytest.mark.asyncio
async def test_write_tools_are_unreachable_without_live_mode(live, monkeypatch):
    """P5-11: with writes off/shadow, the same call is refused outright."""
    monkeypatch.setenv("SMARTCS_WRITE_MODE", "shadow")
    seed_memory_source_event("sess-live", "user_002", "req-live-6", "把进展记到日记")
    before = _daily_line_count(live)

    response = await _post(live, _daily_body("sess-live", "req-live-6"))
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "tool_not_allowed_on_internal_channel"
    assert _daily_line_count(live) == before


@pytest.mark.asyncio
async def test_copilot_action_confirm_without_a_pending_action_is_refused(live):
    """5b: copilot_action_confirm is reachable, but refuses without a valid pending."""
    seed_memory_source_event("sess-live", "user_002", "req-live-7", "确认执行")
    response = await _post(
        live,
        {
            "tool": "copilot_action_confirm",
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


@pytest.mark.asyncio
async def test_smuggled_identity_fields_are_stripped_and_ignored(live):
    seed_memory_source_event("sess-live", "user_002", "req-rc", "把这件事记到日记里")
    await _seed_receipt("sess-live", "req-rc")
    before = _daily_line_count(live)

    body = {
        "tool": "vault_append_daily",
        "arguments": {
            "client_request_id": "FORGED-IDEMPOTENCY-KEY",
            "user_id": "user_999",
            "confirmed": True,
            "entry": "完成了代码审阅",
            "date_str": "2026-11-18",
        },
        "session_id": "sess-live",
        "client_request_id": "req-rc",
    }
    response = await _post(live, body)
    payload = response.json()

    assert payload["details"]["authorized"] is True
    assert payload["details"]["executed"] is True, payload["details"]
    assert sorted(payload["details"]["audit"]["strippedFields"]) == [
        "client_request_id",
        "confirmed",
        "user_id",
    ]
    assert _daily_line_count(live) == before + 1


@pytest.mark.asyncio
async def test_executed_reflects_handler_failure_cleanly(live):
    """An invalid write argument must not report executed=True."""
    seed_memory_source_event("sess-live", "user_002", "req-bf", "记到日记里")
    await _seed_receipt("sess-live", "req-bf")
    before = _daily_line_count(live)

    response = await _post(
        live,
        {
            "tool": "vault_append_daily",
            "arguments": {"entry": "   ", "date_str": "2026-11-18"},
            "session_id": "sess-live",
            "client_request_id": "req-bf",
        },
    )
    payload = response.json()["details"]
    if payload.get("authorized"):
        assert payload["executed"] is False
        assert payload["errorCode"]
    assert _daily_line_count(live) == before


@pytest.mark.asyncio
async def test_cancel_pending_action_transitions_status_and_blocks_confirm(live):
    """Store cancel is atomic, scoped by session+user, idempotent to False, and blocks confirm."""
    db = PlatformDatabase.from_env()
    store = WriteAuthorizationService(db)
    created = await store.create_pending_action(
        session_id=live["session_id"],
        user_id=live["business_user_id"],
        action_type="tool",
        payload={
            "tool": "mail_send",
            "arguments": {
                "account": "163",
                "to": "alice@example.com",
                "subject": "Weekly Sync",
                "body": "Cancel test",
            },
        },
    )
    pending_id = created["pending_action_id"]
    try:
        assert (
            await store.cancel_pending_action(
                pending_id,
                session_id="wrong-session",
                user_id=live["business_user_id"],
            )
            is False
        )
        assert (
            await store.cancel_pending_action(
                pending_id,
                session_id=live["session_id"],
                user_id="wrong-user",
            )
            is False
        )
        still_pending = await store.load_pending_action(pending_id)
        assert still_pending is not None and still_pending["status"] == "pending"

        assert (
            await store.cancel_pending_action(
                pending_id,
                session_id=live["session_id"],
                user_id=live["business_user_id"],
            )
            is True
        )
        cancelled_row = await store.load_pending_action(pending_id)
        assert cancelled_row is not None and cancelled_row["status"] == "cancelled"

        assert (
            await store.cancel_pending_action(
                pending_id,
                session_id=live["session_id"],
                user_id=live["business_user_id"],
            )
            is False
        )

        seed_memory_source_event(
            live["session_id"], live["business_user_id"], "req-cancel-confirm", "确认发送"
        )
        with pytest.raises(AuthorizationDenied) as exc:
            await store.authorize_copilot_action_confirm(
                session_id=live["session_id"],
                client_request_id="req-cancel-confirm",
                account_user_id=live["business_user_id"],
                arguments={"pending_action_id": pending_id},
            )
        assert exc.value.code == "pending_action_not_pending"

        confirm_resp = await _post(
            live,
            {
                "tool": "copilot_action_confirm",
                "arguments": {"pending_action_id": pending_id},
                "session_id": live["session_id"],
                "client_request_id": "req-cancel-confirm",
            },
        )
        confirm_details = confirm_resp.json()["details"]
        assert confirm_details["authorized"] is False
        assert confirm_details["executed"] is False
        assert confirm_details["errorCode"] == "pending_action_not_pending"

        # Race window: even if confirm already loaded status='pending' before
        # cancel won, reserve_operation refuses once cancelled and rolls back.
        raced = await store.create_pending_action(
            session_id=live["session_id"],
            user_id=live["business_user_id"],
            action_type="tool",
            payload={"tool": "mail_send", "arguments": {"account": "163", "to": "a@example.com", "subject": "Race", "body": "B"}},
        )
        raced_id = raced["pending_action_id"]
        try:
            assert (
                await store.cancel_pending_action(
                    raced_id,
                    session_id=live["session_id"],
                    user_id=live["business_user_id"],
                )
                is True
            )
            await _seed_receipt(live["session_id"], "req-race-confirm")
            with pytest.raises(AuthorizationDenied) as race_exc:
                await store.reserve_operation(
                    session_id=live["session_id"],
                    client_request_id="req-race-confirm",
                    tool="mail_send",
                    target_hash="race-hash",
                    pending_action_id=raced_id,
                )
            assert race_exc.value.code == "pending_action_not_reserved"
            receipt_row = await db._call(
                lambda _c, cur: (
                    cur.execute(
                        "SELECT open_write_operations FROM agent_run_receipt WHERE session_id=%s AND client_request_id=%s",
                        (live["session_id"], "req-race-confirm"),
                    ),
                    cur.fetchone(),
                )[1]
            )
            ops = receipt_row["open_write_operations"]
            if isinstance(ops, str):
                ops = json.loads(ops)
            assert ops == []
        finally:
            await db._call(
                lambda _c, cur: cur.execute("DELETE FROM pending_action WHERE id=%s", (raced_id,))
            )
    finally:
        await db._call(
            lambda _c, cur: cur.execute("DELETE FROM pending_action WHERE id=%s", (pending_id,))
        )


@pytest.mark.asyncio
async def test_reserved_pending_action_blocks_cancel_and_supports_same_request_retry(live):
    """Order 2: confirm reserves operation_id first -> cancel fails; same-request retry recovers."""
    db = PlatformDatabase.from_env()
    store = WriteAuthorizationService(db)
    payload = {
        "tool": "mail_send",
        "arguments": {
            "account": "163",
            "to": "alice@example.com",
            "subject": "Order 2 Reserve",
            "body": "Body",
        },
    }
    created = await store.create_pending_action(
        session_id=live["session_id"],
        user_id=live["business_user_id"],
        action_type="tool",
        payload=payload,
    )
    pending_id = created["pending_action_id"]
    req_confirm = f"req-reserve-first-{uuid.uuid4().hex[:8]}"
    req_other = f"req-other-confirm-{uuid.uuid4().hex[:8]}"
    try:
        seed_memory_source_event(live["session_id"], live["business_user_id"], req_confirm, "确认执行")
        seed_memory_source_event(live["session_id"], live["business_user_id"], req_other, "确认执行")
        await _seed_receipt(live["session_id"], req_confirm)
        await _seed_receipt(live["session_id"], req_other)

        # Simulate reserve_operation succeeding before execution/consume completes.
        from internal_api.write_authorization import canonical_hash

        target_hash = canonical_hash(payload)
        op_id = await store.reserve_operation(
            session_id=live["session_id"],
            client_request_id=req_confirm,
            tool="mail_send",
            target_hash=target_hash,
            pending_action_id=pending_id,
        )

        # Cancel MUST fail once operation_id has claimed execution authority.
        assert (
            await store.cancel_pending_action(
                pending_id,
                session_id=live["session_id"],
                user_id=live["business_user_id"],
            )
            is False
        )
        row = await store.load_pending_action(pending_id)
        assert row is not None
        assert row["status"] == "pending"
        assert row["operation_id"] == op_id

        # A DIFFERENT client_request_id cannot reserve a second operation_id.
        with pytest.raises(AuthorizationDenied) as other_exc:
            await store.authorize_copilot_action_confirm(
                session_id=live["session_id"],
                client_request_id=req_other,
                account_user_id=live["business_user_id"],
                arguments={"pending_action_id": pending_id},
            )
        assert other_exc.value.code == "pending_action_not_reserved"

        # Same (session_id, client_request_id) retry reuses its own reserved operation_id.
        decision = await store.authorize_copilot_action_confirm(
            session_id=live["session_id"],
            client_request_id=req_confirm,
            account_user_id=live["business_user_id"],
            arguments={"pending_action_id": pending_id},
        )
        assert decision.operation_id == op_id
        assert decision.tool == "mail_send"
    finally:
        await db._call(
            lambda _c, cur: cur.execute("DELETE FROM pending_action WHERE id=%s", (pending_id,))
        )


@pytest.mark.asyncio
async def test_cancel_pending_action_rejects_expired_and_non_tool_entries(live):
    """Expired or non-tool pending entries are never transitioned to cancelled."""
    db = PlatformDatabase.from_env()
    store = WriteAuthorizationService(db)
    expired_id = (
        await store.create_pending_action(
            session_id=live["session_id"],
            user_id=live["business_user_id"],
            action_type="tool",
            payload={"tool": "mail_send", "arguments": {"account": "163", "to": "a@example.com", "subject": "Exp", "body": "B"}},
        )
    )["pending_action_id"]
    refund_id = (
        await store.create_pending_action(
            session_id=live["session_id"],
            user_id=live["business_user_id"],
            action_type="refund_create",
            payload={"order_id": "ORD-1"},
        )
    )["pending_action_id"]
    try:
        past = datetime.now(timezone.utc) - timedelta(minutes=5)
        await db._call(
            lambda _c, cur: cur.execute(
                "UPDATE pending_action SET expires_at=%s WHERE id=%s",
                (past, expired_id),
            )
        )
        assert (
            await store.cancel_pending_action(
                expired_id,
                session_id=live["session_id"],
                user_id=live["business_user_id"],
            )
            is False
        )
        assert (await store.load_pending_action(expired_id))["status"] == "pending"

        assert (
            await store.cancel_pending_action(
                refund_id,
                session_id=live["session_id"],
                user_id=live["business_user_id"],
            )
            is False
        )
        assert (await store.load_pending_action(refund_id))["status"] == "pending"
    finally:
        await db._call(
            lambda _c, cur: cur.execute(
                "DELETE FROM pending_action WHERE id IN (%s,%s)", (expired_id, refund_id)
            )
        )


@pytest.mark.asyncio
async def test_http_confirm_cancel_race_and_reserved_expiry_guard(live, monkeypatch):
    """Prove Order 1 race rollback via HTTP and reserved row immunity to cancel/expire."""
    db = PlatformDatabase.from_env()
    store = WriteAuthorizationService(db)
    payload = {
        "tool": "mail_send",
        "arguments": {
            "account": "163",
            "to": "alice@example.com",
            "subject": "HTTP Mutual Exclusion",
            "body": "Body",
        },
    }
    race_pending_id = (
        await store.create_pending_action(
            session_id=live["session_id"],
            user_id=live["business_user_id"],
            action_type="tool",
            payload=payload,
        )
    )["pending_action_id"]
    reserved_id = (
        await store.create_pending_action(
            session_id=live["session_id"],
            user_id=live["business_user_id"],
            action_type="tool",
            payload=payload,
        )
    )["pending_action_id"]
    req_race = f"req-http-race-{uuid.uuid4().hex[:8]}"
    req_reserved = f"req-http-res-{uuid.uuid4().hex[:8]}"
    req_other = f"req-http-other-{uuid.uuid4().hex[:8]}"
    try:
        # Order 1 via HTTP: cancel wins right after confirm reads pending_action, before reserve_operation.
        seed_memory_source_event(live["session_id"], live["business_user_id"], req_race, "确认执行")
        await _seed_receipt(live["session_id"], req_race)

        original_reserve = WriteAuthorizationService.reserve_operation

        async def cancel_then_reserve(self, **kwargs):
            await store.cancel_pending_action(
                race_pending_id,
                session_id=live["session_id"],
                user_id=live["business_user_id"],
            )
            return await original_reserve(self, **kwargs)

        with monkeypatch.context() as mp:
            mp.setattr(WriteAuthorizationService, "reserve_operation", cancel_then_reserve)
            race_resp = await _post(
                live,
                {
                    "tool": "copilot_action_confirm",
                    "arguments": {"pending_action_id": race_pending_id},
                    "session_id": live["session_id"],
                    "client_request_id": req_race,
                },
            )
        race_details = race_resp.json()["details"]
        assert race_details["authorized"] is False
        assert race_details["executed"] is False
        assert race_details["errorCode"] == "pending_action_not_reserved"
        assert (await store.load_pending_action(race_pending_id))["status"] == "cancelled"

        # Order 2 via HTTP: reserved row cannot be cancelled, expired, or claimed by another request.
        seed_memory_source_event(live["session_id"], live["business_user_id"], req_reserved, "确认执行")
        seed_memory_source_event(live["session_id"], live["business_user_id"], req_other, "确认执行")
        await _seed_receipt(live["session_id"], req_reserved)
        await _seed_receipt(live["session_id"], req_other)

        from internal_api.write_authorization import canonical_hash

        op_id = await store.reserve_operation(
            session_id=live["session_id"],
            client_request_id=req_reserved,
            tool="mail_send",
            target_hash=canonical_hash(payload),
            pending_action_id=reserved_id,
        )
        assert (
            await store.cancel_pending_action(
                reserved_id,
                session_id=live["session_id"],
                user_id=live["business_user_id"],
            )
            is False
        )
        other_resp = await _post(
            live,
            {
                "tool": "copilot_action_confirm",
                "arguments": {"pending_action_id": reserved_id},
                "session_id": live["session_id"],
                "client_request_id": req_other,
            },
        )
        assert other_resp.json()["details"]["errorCode"] == "pending_action_not_reserved"

        past = datetime.now(timezone.utc) - timedelta(minutes=1)
        await db._call(
            lambda _c, cur: cur.execute(
                "UPDATE pending_action SET expires_at=%s WHERE id=%s", (past, reserved_id)
            )
        )
        assert await store.expire_pending_action(reserved_id) is False
        still_reserved = await store.load_pending_action(reserved_id)
        assert still_reserved is not None
        assert still_reserved["status"] == "pending"
        assert still_reserved["operation_id"] == op_id

        # Same-request replay can still recover its reserved operation_id cleanly.
        recovered = await store.authorize_copilot_action_confirm(
            session_id=live["session_id"],
            client_request_id=req_reserved,
            account_user_id=live["business_user_id"],
            arguments={"pending_action_id": reserved_id},
        )
        assert recovered.operation_id == op_id
    finally:
        await db._call(
            lambda _c, cur: cur.execute(
                "DELETE FROM pending_action WHERE id IN (%s,%s)",
                (race_pending_id, reserved_id),
            )
        )
