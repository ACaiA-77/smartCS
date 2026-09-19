"""SessionStore contract and legacy migration tests."""

from __future__ import annotations

import json

import pytest

from memory.session_store import ConversationState, SessionStore
from memory.short_term import ShortTermMemory


def _store() -> SessionStore:
    return SessionStore(ShortTermMemory(redis_url="redis://127.0.0.1:6399/0"))


def _pending() -> dict:
    return {
        "type": "refund_create",
        "order_id": "ORD-1",
        "user_id": "user-1",
        "amount": 10.0,
        "refund_mode": "refund_only",
        "reason": "用户申请退款",
        "idempotency_key": "refund:s:ORD-1",
        "arguments": {"order_id": "ORD-1", "user_id": "user-1", "reason": "用户申请退款"},
    }


@pytest.mark.asyncio
async def test_state_roundtrip_and_updates_are_json_only():
    store = _store()
    state = ConversationState(
        last_intent="order_query",
        accumulated_entities={"order_id": "ORD-1"},
        turn_count=2,
        pending_action=_pending(),
    )

    await store.save_state("s", state)
    restored = await store.get_state("s")
    assert restored.to_dict() == state.to_dict()
    updated = await store.update_state("s", turn_count=3)
    assert updated.turn_count == 3
    assert (await store.get_state("s")).pending_action["order_id"] == "ORD-1"


@pytest.mark.asyncio
async def test_pending_action_arguments_must_match_top_level_for_dict_and_object():
    store = _store()
    mismatched = _pending()
    mismatched["arguments"]["order_id"] = "ORD-other"
    with pytest.raises(ValueError, match="do not match"):
        await store.save_state("dict", {"pending_action": mismatched})

    state = ConversationState(pending_action=_pending())
    state.pending_action["arguments"]["reason"] = "changed after construction"
    with pytest.raises(ValueError, match="do not match"):
        await store.save_state("object", state)

    assert (await store.get_state("dict")).pending_action is None
    assert (await store.get_state("object")).pending_action is None


@pytest.mark.asyncio
async def test_legacy_snapshot_migrates_once_and_invalid_pending_is_skipped():
    short = ShortTermMemory(redis_url="redis://127.0.0.1:6399/0")
    store = SessionStore(short)
    valid = {"last_intent": "order_query", "accumulated_entities": {"order_id": "ORD-1"}, "turn_count": 2, "pending_action": _pending()}
    await short.add_message("s", "system", "[wm_snapshot]" + json.dumps(valid))
    invalid = _pending()
    invalid["arguments"]["order_id"] = "ORD-other"
    await short.add_message(
        "s",
        "system",
        "[wm_snapshot]" + json.dumps({"last_intent": "bad", "pending_action": invalid}),
    )

    migrated = await store.get_state("s")
    assert migrated.last_intent == "order_query"
    assert migrated.pending_action["order_id"] == "ORD-1"
    await short.add_message("s", "system", "[wm_snapshot]" + json.dumps({"last_intent": "older"}))
    assert (await store.get_state("s")).last_intent == "order_query"


@pytest.mark.asyncio
async def test_no_snapshot_history_and_restart_with_new_store():
    short = ShortTermMemory(redis_url="redis://127.0.0.1:6399/0")
    first = SessionStore(short)
    await first.add_message("s", "user", "hello")
    await first.update_state("s", last_intent="knowledge_rag", turn_count=1)

    restarted = SessionStore(short)
    assert (await restarted.get_state("s")).last_intent == "knowledge_rag"
    assert (await restarted.get_history("s"))[0]["content"] == "hello"


@pytest.mark.asyncio
async def test_history_filters_legacy_snapshot_and_clear_removes_both_layers():
    store = _store()
    await store.add_message("s", "user", "hello")
    await store.add_message("s", "system", "[wm_snapshot]{}")
    await store.add_message("s", "assistant", "hi")
    assert [m["role"] for m in await store.get_history("s")] == ["user", "assistant"]
    assert len(await store.get_history("s", include_legacy_system=True)) == 3
    await store.update_state("s", last_intent="x")
    await store.clear("s")
    assert (await store.get_state("s")).last_intent is None
    assert await store.get_history("s") == []


@pytest.mark.asyncio
async def test_api_history_exposes_conversation_messages_only(monkeypatch):
    from api import main as api_main
    from auth.context import UserContext
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    store = _store()
    await store.add_message("api-session", "user", "hello")
    await store.add_message("api-session", "system", "[wm_snapshot]{}")
    monkeypatch.setattr(api_main, "session_store", store)
    monkeypatch.setattr(api_main, "checkpoint_store", None)
    sessions = SimpleNamespace(get_owned=AsyncMock(return_value={"session_id": "api-session", "account_id": 1}))
    monkeypatch.setattr(api_main.app.state, "platform_sessions", sessions, raising=False)
    user = UserContext(1, "history-unit-test", "user_001")

    response = await api_main.get_history("api-session", user)
    sessions.get_owned.assert_awaited_with("api-session", 1)
    assert [item["role"] for item in response["messages"]] == ["user"]
    cleared = await api_main.clear_history("api-session", user)
    assert cleared["cleared"] is True
    assert await store.get_history("api-session") == []
