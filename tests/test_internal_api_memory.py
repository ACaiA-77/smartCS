"""Phase 3 acceptance for POST /internal/memory/enqueue (design §3).

The point of these tests is that the endpoint is a *pass-through*: it must not
weaken `UserMemoryService.process_message`'s provenance re-check, and it must
never take text from the caller.
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
    build_user_memory_service,
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


@pytest_asyncio.fixture
async def outbox():
    apply_migration()
    memory = await build_user_memory_service()
    app = await build_app(user_memory_service=memory)
    account_id = seed_account(random_username("mem"), "user_001")
    other_id = seed_account(random_username("mem-other"), "user_002")
    session_id = "sess-memory"
    seed_session(session_id, account_id, "pi")
    seed_session("sess-memory-other", other_id, "pi")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        yield {
            "http": http,
            "memory": memory,
            "account_id": account_id,
            "other_id": other_id,
            "session_id": session_id,
            "business_user_id": "user_001",
        }


_DEFAULT = object()


def _enqueue(outbox, source_event_id, *, account_id=None, session_id=None, business_user_id=_DEFAULT):
    account_id = outbox["account_id"] if account_id is None else account_id
    session_id = outbox["session_id"] if session_id is None else session_id
    business_user_id = outbox["business_user_id"] if business_user_id is _DEFAULT else business_user_id
    return outbox["http"].post(
        "/internal/memory/enqueue",
        json={
            "session_id": session_id,
            "client_request_id": "req-mem-1",
            "source_event_id": source_event_id,
        },
        headers=service_header(
            account_id=account_id, session_id=session_id, business_user_id=business_user_id
        ),
    )


@pytest.mark.asyncio
async def test_enqueues_the_durable_provenance_row(outbox):
    event_id = seed_memory_source_event(
        "sess-memory", "user_001", "req-mem-1", "我叫张伟，手机号是我的常用联系方式。"
    )
    response = await _enqueue(outbox, event_id)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["enqueued"] is True
    # process_message returns an enqueue report; the endpoint passes it through.
    assert isinstance(body["result"], dict)


@pytest.mark.asyncio
async def test_unknown_event_id_is_404(outbox):
    response = await _enqueue(outbox, "00000000-0000-0000-0000-000000000000")
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "source_event_not_found"


@pytest.mark.asyncio
async def test_another_users_event_cannot_be_enqueued(outbox):
    """P3-2 (outbox side): the provenance lookup is owner-scoped."""
    other_event = seed_memory_source_event("sess-memory-other", "user_002", "req-mem-1", "我是 user_002")
    # user_001 tries to enqueue user_002's event.
    response = await _enqueue(outbox, other_event)
    assert response.status_code == 404

    # Even naming the other session is refused: it is not user_001's session.
    response = await _enqueue(outbox, other_event, session_id="sess-memory-other")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_missing_business_user_id_is_refused(outbox):
    event_id = seed_memory_source_event("sess-memory", "user_001", "req-mem-1", "你好")
    response = await _enqueue(outbox, event_id, business_user_id=None)
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_caller_cannot_supply_text(outbox):
    """The body schema forbids extra fields, so text can only come from the DB."""
    event_id = seed_memory_source_event("sess-memory", "user_001", "req-mem-1", "原始内容")
    response = await outbox["http"].post(
        "/internal/memory/enqueue",
        json={
            "session_id": "sess-memory",
            "client_request_id": "req-mem-1",
            "source_event_id": event_id,
            "content": "注入的文本，不应被采用",
        },
        headers=service_header(
            account_id=outbox["account_id"], session_id="sess-memory", business_user_id="user_001"
        ),
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_enqueue_is_idempotent(outbox):
    """Retrying the same event must not double-insert candidates."""
    event_id = seed_memory_source_event("sess-memory", "user_001", "req-mem-1", "我的邮箱是 zhang@example.com")
    first = await _enqueue(outbox, event_id)
    second = await _enqueue(outbox, event_id)
    assert first.status_code == 200 and second.status_code == 200

    from tests.internal_api_helpers import connect

    connection = connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*) FROM user_memory_candidate WHERE source_event_id = %s", (event_id,)
            )
            count = cursor.fetchone()[0]
    finally:
        connection.close()
    assert count <= 1, "candidate inserts must be idempotent per source event"
