"""Phase 1 §7: harness-aware history dispatch in api/main.py.

Moved into python-impl/tests/ in Phase 2 (D8 裁决). Exercises the REAL api.main
handlers with only the harness HTTP call intercepted, so the dispatch decision
itself (legacy vs pi) is what is under test.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

import api.main as main
from auth.context import UserContext

USER = UserContext(account_id=1, username="u", business_user_id="bu-1")


class _FakeResponse:
    def __init__(self, status_code: int, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


@pytest.fixture
def spy(monkeypatch):
    calls = []

    async def fake_call_harness(method, path, **kwargs):
        calls.append({"method": method, "path": path, **kwargs})
        if method == "DELETE":
            return _FakeResponse(200, {"deleted": True})
        return _FakeResponse(200, {"messages": [{"role": "user", "content": "来自 Pi 的历史", "created_at": "t"}]})

    monkeypatch.setattr(main, "call_harness", fake_call_harness)
    return calls


def _owned(harness_version: str):
    async def fake_owned(session_id, user):
        return {"session_id": session_id, "account_id": user.account_id, "harness_version": harness_version}

    return fake_owned


@pytest.mark.asyncio
async def test_pi_session_history_is_forwarded_to_the_harness(monkeypatch, spy):
    monkeypatch.setattr(main, "_owned_session", _owned("pi"))
    result = await main.get_history("sess-pi", USER)

    assert result == {
        "session_id": "sess-pi",
        "messages": [{"role": "user", "content": "来自 Pi 的历史", "created_at": "t"}],
    }
    assert len(spy) == 1
    assert spy[0]["method"] == "GET"
    assert spy[0]["path"] == "/internal/history/sess-pi"
    assert spy[0]["session_id"] == "sess-pi"
    assert spy[0]["business_user_id"] == "bu-1"


@pytest.mark.asyncio
async def test_legacy_session_history_never_touches_the_harness(monkeypatch, spy):
    monkeypatch.setattr(main, "_owned_session", _owned("legacy"))

    async def fake_history(session_id, user_id):
        return [{"role": "user", "content": "legacy 历史"}]

    monkeypatch.setattr(main, "checkpoint_store", type("S", (), {"history": staticmethod(fake_history)})())
    result = await main.get_history("sess-legacy", USER)

    assert result["messages"] == [{"role": "user", "content": "legacy 历史"}]
    assert spy == []


@pytest.mark.asyncio
async def test_pi_session_delete_forwards_and_maps_conflict(monkeypatch, spy):
    monkeypatch.setattr(main, "_owned_session", _owned("pi"))
    result = await main.clear_history("sess-pi", USER)
    assert result == {"session_id": "sess-pi", "cleared": True}
    assert spy[0]["method"] == "DELETE"

    async def busy_call(method, path, **kwargs):
        return _FakeResponse(409)

    monkeypatch.setattr(main, "call_harness", busy_call)
    with pytest.raises(main.CheckpointConflict):
        await main.clear_history("sess-pi", USER)


@pytest.mark.asyncio
async def test_harness_unavailable_surfaces_as_503(monkeypatch):
    monkeypatch.setattr(main, "_owned_session", _owned("pi"))

    async def down(method, path, **kwargs):
        raise main.HarnessUnavailable("nope")

    monkeypatch.setattr(main, "call_harness", down)
    with pytest.raises(HTTPException) as excinfo:
        await main.get_history("sess-pi", USER)
    assert excinfo.value.status_code == 503
