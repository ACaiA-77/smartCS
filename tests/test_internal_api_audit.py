"""Phase 6 acceptance: audit ingest + trace propagation (design §1/§3/§4).

P6-2 (audit idempotency) lives here, together with the trace-context contract
that P6-1 asserts end to end from the harness side.
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from internal_api.audit import (
    MAX_EVENTS_PER_BATCH,
    parse_traceparent,
    reset_trace_records,
    trace_records,
)
from tests.internal_api_helpers import (
    SERVICE_SECRET,
    TEST_DATABASE,
    USER_SECRET,
    apply_migration,
    build_app,
    random_username,
    seed_account,
    seed_session,
    service_header,
)

TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
PARENT_SPAN_ID = "00f067aa0ba902b7"


@pytest.fixture(autouse=True)
def _environment(monkeypatch):
    monkeypatch.setenv("INTERNAL_SERVICE_JWT_SECRET", SERVICE_SECRET)
    monkeypatch.setenv("AUTH_JWT_SECRET", USER_SECRET)
    monkeypatch.setenv("MYSQL_DATABASE", TEST_DATABASE)
    monkeypatch.setenv("SMARTCS_INTERNAL_TRACE_RECORDS", "1")


@pytest_asyncio.fixture
async def live(tmp_path):
    from platform_db.database import PlatformDatabase

    apply_migration()
    reset_trace_records()
    app = await build_app()
    app.state.platform_database = PlatformDatabase.from_env()
    account_id = seed_account(random_username("p6audit"), "user_002")
    session_id = "sess-audit"
    seed_session(session_id, account_id, "pi")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        yield {
            "http": http,
            "app": app,
            "account_id": account_id,
            "session_id": session_id,
            "business_user_id": "user_002",
        }


def _event(**overrides):
    event = {
        "event_id": str(uuid.uuid4()),
        "kind": "tool_call",
        "tool_name": "order_query",
        "tool_call_id": "call-1",
        "operation_id": None,
        "trace_id": TRACE_ID,
        "payload": {"occurred_at": "2026-10-04T00:00:00.000Z", "input": {"order_id": "ORD-1"}},
    }
    event.update(overrides)
    return event


def _post_batch(
    live,
    events,
    *,
    request_id="req-audit",
    token_request_id=None,
    account_id=None,
    extra_headers=None,
):
    """POST one batch; the token's request id can be pointed at a different one."""
    headers = service_header(
        account_id=account_id or live["account_id"],
        session_id=live["session_id"],
        business_user_id=live["business_user_id"],
        client_request_id=token_request_id or request_id,
    )
    headers.update(extra_headers or {})
    return live["http"].post(
        "/internal/audit",
        json={
            "session_id": live["session_id"],
            "client_request_id": request_id,
            "events": events,
        },
        headers=headers,
    )


def _query(sql: str, params: tuple) -> tuple | None:
    import os

    import pymysql

    connection = pymysql.connect(
        host=os.getenv("MYSQL_HOST", "127.0.0.1"),
        port=int(os.getenv("MYSQL_PORT", "3307")),
        user=os.getenv("MYSQL_USER", "smartcs"),
        password=os.getenv("MYSQL_PASSWORD", ""),
        database=TEST_DATABASE,
        charset="utf8mb4",
        autocommit=True,
    )
    try:
        with connection.cursor() as cursor:
            cursor.execute(sql, params)
            return cursor.fetchone()
    finally:
        connection.close()


def _audit_count(live) -> int:
    return int(_query("SELECT COUNT(*) FROM audit_event WHERE session_id=%s", (live["session_id"],))[0])


# --- P6-2: idempotency ------------------------------------------------------


@pytest.mark.asyncio
async def test_p6_2_resending_the_same_batch_inserts_nothing(live):
    """P6-2: event_id is the idempotency key; a replay must not add rows."""
    events = [_event(tool_name="order_query"), _event(kind="tool_result", tool_name="order_query")]

    first = await _post_batch(live, events, request_id="req-p62")
    assert first.status_code == 200, first.text
    assert first.json() == {"inserted": 2, "duplicates": 0}
    assert _audit_count(live) == 2

    # Same event ids, byte-for-byte the same batch: the unique key absorbs it.
    replay = await _post_batch(live, events, request_id="req-p62")
    assert replay.status_code == 200, replay.text
    assert replay.json() == {"inserted": 0, "duplicates": 2}
    assert _audit_count(live) == 2, "a repeated batch must not grow the table"

    # A NEW event in the same request still lands — idempotency is per event.
    third = await _post_batch(live, [_event(tool_name="refund_evaluate")], request_id="req-p62")
    assert third.json() == {"inserted": 1, "duplicates": 0}
    assert _audit_count(live) == 3


@pytest.mark.asyncio
async def test_audit_stores_the_authoritative_identity_not_the_body(live):
    """Session/request ids come from the verified token, and must agree."""
    event = _event()
    ok = await _post_batch(live, [event], request_id="req-bind")
    assert ok.status_code == 200

    row = _query(
        "SELECT session_id, client_request_id, trace_id, kind, tool_name FROM audit_event WHERE event_id=%s",
        (event["event_id"],),
    )
    assert row is not None
    assert row[0] == live["session_id"]
    assert row[1] == "req-bind"
    assert row[2] == TRACE_ID
    assert row[3] == "tool_call"
    assert row[4] == "order_query"


# --- the endpoint's own contract --------------------------------------------


@pytest.mark.asyncio
async def test_audit_requires_service_auth_and_matching_request_identity(live):
    anonymous = await live["http"].post(
        "/internal/audit",
        json={"session_id": live["session_id"], "client_request_id": "req-x", "events": [_event()]},
    )
    assert anonymous.status_code == 401

    # A valid token for a DIFFERENT request id cannot author this batch.
    mismatched = await _post_batch(live, [_event()], request_id="req-other", token_request_id="req-token")
    assert mismatched.status_code == 401
    assert _audit_count(live) == 0

    # Another account's session is refused before anything is written.
    other = await _post_batch(live, [_event()], request_id="req-third", account_id=live["account_id"] + 1)
    assert other.status_code in (401, 403, 404)
    assert _audit_count(live) == 0


@pytest.mark.asyncio
async def test_audit_rejects_bad_kind_oversized_payload_and_oversized_batch(live):
    bad_kind = await _post_batch(live, [_event() | {"kind": "something_else"}])
    assert bad_kind.status_code == 422

    huge = await _post_batch(live, [_event(payload={"blob": "x" * 20_000})])
    assert huge.status_code == 413

    too_many = await _post_batch(live, [_event() for _ in range(MAX_EVENTS_PER_BATCH + 1)])
    assert too_many.status_code == 422
    assert _audit_count(live) == 0


# --- trace propagation ------------------------------------------------------


def test_traceparent_parsing_is_strict():
    assert parse_traceparent(f"00-{TRACE_ID}-{PARENT_SPAN_ID}-01") == {
        "trace_id": TRACE_ID,
        "parent_span_id": PARENT_SPAN_ID,
        "sampled": True,
    }
    assert parse_traceparent(f"00-{TRACE_ID}-{PARENT_SPAN_ID}-00")["sampled"] is False
    # Malformed, reserved and forbidden forms are all "no parent".
    assert parse_traceparent(None) is None
    assert parse_traceparent("") is None
    assert parse_traceparent("00-abc-def-01") is None
    assert parse_traceparent(f"00-{'0' * 32}-{PARENT_SPAN_ID}-01") is None
    assert parse_traceparent(f"00-{TRACE_ID}-{'0' * 16}-01") is None
    assert parse_traceparent(f"ff-{TRACE_ID}-{PARENT_SPAN_ID}-01") is None
    assert parse_traceparent(f"00-{TRACE_ID.upper()}-{PARENT_SPAN_ID}-01")["trace_id"] == TRACE_ID


@pytest.mark.asyncio
async def test_p6_1_python_spans_carry_the_propagated_trace_and_six_ids(live):
    """The Python half of P6-1: the internal span belongs to the TS trace."""
    headers = service_header(
        account_id=live["account_id"],
        session_id=live["session_id"],
        business_user_id=live["business_user_id"],
        client_request_id="req-trace",
    )
    headers.update(
        {
            "traceparent": f"00-{TRACE_ID}-{PARENT_SPAN_ID}-01",
            "x-smartcs-agent-run-id": "4242",
            "x-smartcs-tool-call-id": "call-abc",
            "x-smartcs-operation-id": "op-xyz",
        }
    )
    response = await live["http"].post(
        "/internal/audit",
        json={
            "session_id": live["session_id"],
            "client_request_id": "req-trace",
            "events": [_event()],
        },
        headers=headers,
    )
    assert response.status_code == 200, response.text
    # The observed trace id is echoed back so a caller can correlate logs.
    assert response.headers.get("x-smartcs-trace-id") == TRACE_ID

    records = trace_records(live["session_id"])
    assert records, "the dependency must record this request"
    record = records[-1]
    assert record["trace_id"] == TRACE_ID
    assert record["parent_span_id"] == PARENT_SPAN_ID
    assert record["path"] == "/internal/audit"
    # The six-id family, as far as this hop can legitimately know it.
    assert record["session_id"] == live["session_id"]
    assert record["client_request_id"] == "req-trace"
    assert record["agent_run_id"] == "4242"
    assert record["tool_call_id"] == "call-abc"
    assert record["operation_id"] == "op-xyz"
    assert record["outcome"] == "ok"
    assert record["duration_ms"] >= 0


@pytest.mark.asyncio
async def test_absent_or_malformed_traceparent_still_records_a_fresh_trace(live):
    call = await _post_batch(live, [_event()], request_id="req-notrace", extra_headers={"traceparent": "garbage"})
    assert call.status_code == 200
    assert len(call.headers.get("x-smartcs-trace-id", "")) == 32

    record = trace_records(live["session_id"])[-1]
    assert len(record["trace_id"]) == 32
    assert record["trace_id"] != "garbage"
    assert record["parent_span_id"] is None
    assert record["agent_run_id"] is None


@pytest.mark.asyncio
async def test_failed_requests_are_recorded_with_their_status(live):
    """A refusal is still a span; observability must not depend on success."""
    response = await _post_batch(live, [_event() | {"kind": "nope"}], request_id="req-bad")
    assert response.status_code == 422
    record = trace_records(live["session_id"])[-1]
    assert record["outcome"] == "error"
    assert record["status_code"] == 422


@pytest.mark.asyncio
async def test_trace_records_endpoint_is_gated_and_session_scoped(live, monkeypatch):
    """The diagnostic read is off by default and only shows one session."""
    headers = service_header(
        account_id=live["account_id"],
        session_id=live["session_id"],
        business_user_id=live["business_user_id"],
        client_request_id="req-read",
    )
    await _post_batch(live, [_event()], request_id="req-read")

    monkeypatch.delenv("SMARTCS_INTERNAL_TRACE_RECORDS", raising=False)
    disabled = await live["http"].get("/internal/trace/records", headers=headers)
    assert disabled.status_code == 404

    monkeypatch.setenv("SMARTCS_INTERNAL_TRACE_RECORDS", "1")
    enabled = await live["http"].get("/internal/trace/records", headers=headers)
    assert enabled.status_code == 200
    body = enabled.json()
    assert body["session_id"] == live["session_id"]
    assert all(item["session_id"] == live["session_id"] for item in body["records"])

    unauthenticated = await live["http"].get("/internal/trace/records")
    assert unauthenticated.status_code == 401
