"""Phase 7 §2/§5: the unified-entry chat dispatch in api/main.py.

Everything here drives the REAL `api.main.chat` handler against the REAL MySQL
session table. Only two things are substituted, and neither is the code under
test:

* the harness HTTP hop (`main.forward_chat`) — the same boundary Phase 1's
  history-dispatch test intercepts — so the dispatch decision, the forwarded
  arguments and the response adaptation are what is asserted;
* the legacy orchestrator, replaced by a tripwire (`_TripwireOrchestrator`)
  that fails the test if a pi session ever reaches it. That tripwire is the
  point of P7-6: a degraded fallback would look like a passing request.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from starlette.requests import Request

import api.main as main
from auth.context import UserContext
from internal_api.harness_client import harness_version_for_account
from platform_db.sessions import Sessions
from tests.internal_api_helpers import (
    apply_migration,
    platform_database,
    random_username,
    seed_account,
    seed_session,
)

TOKEN = "header.payload.signature"

_INVALID_JSON = object()

PI_SESSION_ID = "sess-pi-dispatch"
LEGACY_SESSION_ID = "sess-legacy-dispatch"


class _FakeHarnessResponse:
    def __init__(self, status_code: int, payload=None):
        self.status_code = status_code
        self._payload = {} if payload is None else payload

    def json(self):
        if self._payload is _INVALID_JSON:
            raise ValueError("not json")
        return self._payload


class _TripwireOrchestrator:
    """Fails loudly if a pi session is ever routed to the legacy path."""

    def __init__(self):
        self.calls = 0

    async def ainvoke(self, _state):
        self.calls += 1
        raise AssertionError("legacy orchestrator was invoked for a pi session")


class _StubOrchestrator:
    def __init__(self, response="legacy answer", intent="order_query"):
        self.calls = 0
        self._response = response
        self._intent = intent

    async def ainvoke(self, _state):
        self.calls += 1
        return {"final_response": self._response, "intent": self._intent, "compliance_passed": True}


class _MemorySessionStore:
    def __init__(self):
        self.messages: dict[str, list[dict]] = {}

    async def add_message(self, session_id, role, content):
        self.messages.setdefault(session_id, []).append({"role": role, "content": content})

    async def get_history(self, session_id):
        return list(self.messages.get(session_id, []))


def _http_request(token: str | None = TOKEN, bearer: str | None = None) -> Request:
    headers = []
    if token is not None:
        headers.append((b"cookie", f"smartcs_auth={token}".encode()))
    if bearer is not None:
        headers.append((b"authorization", f"Bearer {bearer}".encode()))
    return Request({"type": "http", "method": "POST", "path": "/api/chat", "headers": headers,
                    "query_string": b""})


async def _chat(message="你好", session_id=None, client_request_id=None, user_token=TOKEN, user=None):
    """Drive the real handler the way FastAPI does: business args + injected token."""
    return await main.chat(
        main.ChatRequest(message=message, session_id=session_id, client_request_id=client_request_id),
        user,
        user_token,
    )


@pytest.fixture
async def sessions(monkeypatch):
    apply_migration()
    database = platform_database()
    await database.initialize()
    handle = Sessions(database)
    monkeypatch.setattr(main.app.state, "platform_sessions", handle, raising=False)
    return handle


@pytest.fixture
async def pi_account(sessions):
    """A real account holding one pi session; `user` matches it exactly."""
    business_user_id = random_username("bu-pi")
    account_id = seed_account(random_username("dispatch-pi"), business_user_id)
    seed_session(PI_SESSION_ID, account_id, "pi")
    return UserContext(account_id=account_id, username="pi-user", business_user_id=business_user_id)


@pytest.fixture
async def legacy_account(sessions):
    business_user_id = random_username("bu-legacy")
    account_id = seed_account(random_username("dispatch-legacy"), business_user_id)
    seed_session(LEGACY_SESSION_ID, account_id, "legacy")
    return UserContext(account_id=account_id, username="legacy-user", business_user_id=business_user_id)


@pytest.fixture
def forwarded(monkeypatch):
    """Spy for the harness hop; responds like a healthy harness by default."""
    calls: list[dict] = []

    async def fake_forward_chat(**kwargs):
        calls.append(kwargs)
        return _FakeHarnessResponse(
            200,
            {
                "session_id": kwargs["session_id"],
                "client_request_id": kwargs["client_request_id"],
                "message": {"role": "assistant", "content": "来自 Pi 的回答"},
                "replayed": False,
                "intent_label": "refund",
            },
        )

    monkeypatch.setattr(main, "forward_chat", fake_forward_chat)
    return calls


@pytest.mark.asyncio
async def test_pi_session_turn_is_forwarded_and_adapted(pi_account, forwarded, monkeypatch):
    tripwire = _TripwireOrchestrator()
    monkeypatch.setattr(main, "chat_orchestrator", tripwire)

    result = await _chat(session_id=PI_SESSION_ID, client_request_id="req-1", user=pi_account)

    assert tripwire.calls == 0
    assert forwarded == [{
        "account_id": pi_account.account_id,
        "business_user_id": pi_account.business_user_id,
        "session_id": PI_SESSION_ID,
        "client_request_id": "req-1",
        "message": "你好",
        "user_token": TOKEN,
    }]
    assert result.response == "来自 Pi 的回答"
    assert result.session_id == PI_SESSION_ID
    assert result.intent == "refund"
    assert result.compliance_passed is True
    assert result.client_request_id == "req-1"
    assert result.harness_version == "pi"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "http_request,expected",
    [
        (_http_request(token=TOKEN), TOKEN),
        (_http_request(token=None, bearer="bearer.token.here"), "bearer.token.here"),
    ],
    ids=["cookie", "bearer"],
)
async def test_the_caller_token_is_read_from_the_request_it_came_in_on(http_request, expected):
    """The injected token is exactly what `get_current_user` accepted: the
    HttpOnly cookie, or a Bearer header that must agree with it."""
    assert await main._forward_user_token(http_request) == expected


@pytest.mark.asyncio
async def test_the_forwarded_token_is_what_the_client_presented(pi_account, forwarded, monkeypatch):
    monkeypatch.setattr(main, "chat_orchestrator", _TripwireOrchestrator())
    await _chat(session_id=PI_SESSION_ID, user=pi_account, user_token="bearer.token.here")
    assert forwarded[0]["user_token"] == "bearer.token.here"


@pytest.mark.asyncio
async def test_a_missing_client_request_id_is_generated_for_the_harness(pi_account, forwarded, monkeypatch):
    """The harness requires a non-empty request id for its receipt log; the
    public API still accepts its absence."""
    monkeypatch.setattr(main, "chat_orchestrator", _TripwireOrchestrator())
    result = await _chat(session_id=PI_SESSION_ID, user=pi_account)
    generated = forwarded[0]["client_request_id"]
    assert isinstance(generated, str) and len(generated) == 36
    assert result.client_request_id == generated


@pytest.mark.asyncio
async def test_legacy_session_turn_never_reaches_the_harness(legacy_account, forwarded, monkeypatch):
    """P7-1 regression: the legacy chain is untouched by Phase 7."""
    stub = _StubOrchestrator()
    monkeypatch.setattr(main, "chat_orchestrator", stub)
    monkeypatch.setattr(main, "session_store", _MemorySessionStore())

    result = await _chat(session_id=LEGACY_SESSION_ID, client_request_id="req-legacy", user=legacy_account)

    assert forwarded == []
    assert stub.calls == 1
    assert result.response == "legacy answer"
    assert result.intent == "order_query"
    assert result.harness_version == "legacy"


@pytest.mark.asyncio
async def test_an_existing_legacy_session_stays_legacy_at_percent_100(
    monkeypatch, legacy_account, forwarded
):
    """P7-4: the rollout decides NEW sessions only. An existing legacy session
    is not migrated — that would split one conversation across two transcripts."""
    monkeypatch.setenv("SMARTCS_PI_ROLLOUT_PERCENT", "100")
    monkeypatch.setattr(main, "chat_orchestrator", _StubOrchestrator())
    monkeypatch.setattr(main, "session_store", _MemorySessionStore())

    result = await _chat(session_id=LEGACY_SESSION_ID, user=legacy_account)

    assert forwarded == []
    assert result.harness_version == "legacy"


@pytest.mark.asyncio
async def test_a_new_session_at_percent_100_is_created_as_pi_and_forwarded(
    monkeypatch, sessions, pi_account, forwarded
):
    """P7-2 at the Python layer: bucket -> create -> forward, one entry point."""
    monkeypatch.setenv("SMARTCS_PI_ROLLOUT_PERCENT", "100")
    monkeypatch.setattr(main, "chat_orchestrator", _TripwireOrchestrator())

    result = await _chat(message="帮我退款", client_request_id="first-turn", user=pi_account)

    assert forwarded[0]["session_id"] == result.session_id
    assert forwarded[0]["message"] == "帮我退款"
    assert forwarded[0]["client_request_id"] == "first-turn"
    stored = await sessions.get_owned(result.session_id, pi_account.account_id)
    assert stored["harness_version"] == "pi"
    assert stored["title"] == "帮我退款"
    assert result.harness_version == "pi"


@pytest.mark.asyncio
async def test_a_new_session_at_percent_0_is_created_legacy(
    monkeypatch, sessions, legacy_account, forwarded
):
    monkeypatch.setenv("SMARTCS_PI_ROLLOUT_PERCENT", "0")
    monkeypatch.setattr(main, "chat_orchestrator", _StubOrchestrator())
    monkeypatch.setattr(main, "session_store", _MemorySessionStore())

    result = await _chat(message="你好", user=legacy_account)

    assert forwarded == []
    stored = await sessions.get_owned(result.session_id, legacy_account.account_id)
    assert stored["harness_version"] == "legacy"
    assert result.harness_version == "legacy"


@pytest.mark.asyncio
async def test_harness_unavailable_answers_503_and_never_degrades(
    monkeypatch, sessions, pi_account, forwarded
):
    """P7-6: unreachable harness -> 503, and the legacy orchestrator is NOT a
    fallback. Recovery is a retry of the same request, not a second transcript."""
    monkeypatch.setenv("SMARTCS_PI_ROLLOUT_PERCENT", "100")
    tripwire = _TripwireOrchestrator()
    monkeypatch.setattr(main, "chat_orchestrator", tripwire)

    async def down(**_kwargs):
        raise main.HarnessUnavailable("pi harness unreachable")

    monkeypatch.setattr(main, "forward_chat", down)
    with pytest.raises(HTTPException) as excinfo:
        await _chat(message="退款", client_request_id="outage-turn", user=pi_account)
    assert excinfo.value.status_code == 503
    assert tripwire.calls == 0

    # The session was created (pi) before the hop failed; retrying the same
    # request once the harness is back succeeds on that same session.
    rows = await sessions.list_owned(pi_account.account_id)
    assert {row["harness_version"] for row in rows} == {"pi"}
    created = [row for row in rows if row["session_id"] != PI_SESSION_ID]
    assert len(created) == 1
    session_id = created[0]["session_id"]

    async def back(**kwargs):
        forwarded.append(kwargs)
        return _FakeHarnessResponse(
            200,
            {"session_id": kwargs["session_id"], "client_request_id": kwargs["client_request_id"],
             "message": {"role": "assistant", "content": "恢复后的回答"}, "intent_label": "refund"},
        )

    monkeypatch.setattr(main, "forward_chat", back)
    retried = await _chat(message="退款", session_id=session_id, client_request_id="outage-turn", user=pi_account)
    assert retried.response == "恢复后的回答"
    assert retried.harness_version == "pi"
    assert tripwire.calls == 0


@pytest.mark.asyncio
async def test_retryable_harness_refusals_are_passed_through(pi_account, forwarded, monkeypatch):
    """A healthy harness refusing a turn (unfinished run / session busy) is a
    409/429 for the caller to retry — not a 502 and not a fallback."""
    monkeypatch.setattr(main, "chat_orchestrator", _TripwireOrchestrator())

    for status, detail in ((409, "写操作结果未确定（账簿未能给出结论）"), (429, "session busy")):
        async def refuse(_status=status, _detail=detail, **_kwargs):
            return _FakeHarnessResponse(_status, {"detail": _detail})

        monkeypatch.setattr(main, "forward_chat", refuse)
        with pytest.raises(HTTPException) as excinfo:
            await _chat(session_id=PI_SESSION_ID, user=pi_account)
        assert excinfo.value.status_code == status
        assert excinfo.value.detail == detail


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        _FakeHarnessResponse(500, {"detail": "internal error"}),
        _FakeHarnessResponse(401, {"detail": "invalid authentication"}),
        _FakeHarnessResponse(200, _INVALID_JSON),
        _FakeHarnessResponse(200, {"session_id": "s"}),
        _FakeHarnessResponse(200, {"message": {"role": "assistant", "content": ""}}),
    ],
    ids=["500", "401", "invalid-json", "missing-message", "empty-content"],
)
async def test_broken_harness_answers_are_502(pi_account, forwarded, monkeypatch, response):
    monkeypatch.setattr(main, "chat_orchestrator", _TripwireOrchestrator())

    async def broken(**_kwargs):
        return response

    monkeypatch.setattr(main, "forward_chat", broken)
    with pytest.raises(HTTPException) as excinfo:
        await _chat(session_id=PI_SESSION_ID, user=pi_account)
    assert excinfo.value.status_code == 502


@pytest.mark.asyncio
async def test_explicit_session_creation_uses_the_same_bucket(monkeypatch, sessions, legacy_account):
    """POST /api/sessions is the other creation door and must agree with the
    chat-path one."""
    monkeypatch.setenv("SMARTCS_PI_ROLLOUT_PERCENT", "100")
    created = await main.create_session(legacy_account)
    assert created["harness_version"] == "pi"

    monkeypatch.setenv("SMARTCS_PI_ROLLOUT_PERCENT", "0")
    assert (await main.create_session(legacy_account))["harness_version"] == "legacy"


@pytest.mark.asyncio
async def test_repeated_creations_for_one_account_never_drift(monkeypatch, sessions, legacy_account):
    """P7-3 through the real creation door: the verdict is a property of the
    account, so a second (and fifth) session lands in the same cohort."""
    monkeypatch.setenv("SMARTCS_PI_ROLLOUT_PERCENT", "50")
    expected = harness_version_for_account(legacy_account.account_id)
    versions = {(await main.create_session(legacy_account))["harness_version"] for _ in range(5)}
    assert versions == {expected}


@pytest.mark.asyncio
async def test_mixed_cohorts_route_independently(monkeypatch, sessions, forwarded):
    """P7-5: at 50% both cohorts exist and each account's session follows its
    own pin — no cross-talk."""
    # The bucket is a property of the database-assigned account id, so seed
    # accounts until one of each cohort exists (the 50% split makes this
    # immediate; the bound only guards against a broken hash).
    monkeypatch.setenv("SMARTCS_PI_ROLLOUT_PERCENT", "50")
    users: dict[str, UserContext] = {}
    for _ in range(80):
        if len(users) == 2:
            break
        business_user_id = random_username("bu-mixed")
        account_id = seed_account(random_username("mixed"), business_user_id)
        verdict = harness_version_for_account(account_id)
        users.setdefault(verdict, UserContext(
            account_id=account_id, username=f"mixed-{verdict}", business_user_id=business_user_id))
    assert set(users) == {"pi", "legacy"}

    rows = {}
    for verdict, user in users.items():
        created = await sessions.create(user.account_id, harness_version=verdict)
        assert created["harness_version"] == verdict
        rows[verdict] = created["session_id"]

    stub = _StubOrchestrator()
    monkeypatch.setattr(main, "chat_orchestrator", stub)
    monkeypatch.setattr(main, "session_store", _MemorySessionStore())

    pi_result = await _chat(session_id=rows["pi"], user=users["pi"])
    legacy_result = await _chat(session_id=rows["legacy"], user=users["legacy"])

    assert pi_result.harness_version == "pi"
    assert legacy_result.harness_version == "legacy"
    assert [call["session_id"] for call in forwarded] == [rows["pi"]]
    assert stub.calls == 1
