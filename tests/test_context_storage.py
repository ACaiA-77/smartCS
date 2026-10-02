"""Deterministic checkpoint contracts plus explicitly opt-in MySQL storage tests."""

from __future__ import annotations

import asyncio
import contextvars
import json
import os
import threading
import uuid
from typing import Any

import pytest

from checkpoint.models import (
    AgentCheckpoint,
    CheckpointConflict,
    CheckpointCorrupt,
    CheckpointOwnershipError,
)
from checkpoint.store import CheckpointStore


USER_ID = "context-storage-test-owner"


def test_checkpoint_legacy_defaults_cursor_and_unknown_snapshot_fields_fail_closed():
    checkpoint = AgentCheckpoint(session_id=str(uuid.uuid4()), user_id=USER_ID)
    assert checkpoint.last_event_seq == 0

    raw = checkpoint.model_dump()
    raw["snapshot_version"] = 999
    with pytest.raises(CheckpointCorrupt):
        CheckpointStore._decode(
            {
                "user_id": USER_ID,
                "session_id": checkpoint.session_id,
                "version": checkpoint.version,
                "status": checkpoint.status,
                "last_event_seq": 0,
                "state_json": json.dumps(raw),
            },
            USER_ID,
        )


@pytest.fixture
async def mysql_store():
    if os.getenv("SMARTCS_CHECKPOINT_MYSQL_TEST") != "1":
        pytest.skip("real MySQL disabled; set SMARTCS_CHECKPOINT_MYSQL_TEST=1 explicitly")
    from dotenv import load_dotenv

    load_dotenv()
    store = CheckpointStore.from_env()
    await store.initialize()
    session_id = str(uuid.uuid4())
    yield store, session_id

    checkpoint = await store.load(session_id, USER_ID)
    if checkpoint is not None:
        if checkpoint.status == "running":
            terminal = AgentCheckpoint.model_validate(
                {
                    **checkpoint.model_dump(),
                    "current_stage": "FINISHED",
                    "status": "finished",
                    "pending_action": None,
                    "context": {},
                }
            )
            checkpoint = await store.update(terminal)
        await store.delete(session_id, USER_ID, checkpoint.version)


async def _event_count(store: CheckpointStore, session_id: str) -> int:
    return await store._call(
        lambda _connection, cursor: (
            cursor.execute("SELECT COUNT(*) AS count FROM conversation_event WHERE session_id=%s", (session_id,)),
            int(cursor.fetchone()["count"]),
        )[1]
    )


async def _message_events(store: CheckpointStore, session_id: str) -> list[dict[str, Any]]:
    return [
        event for event in await store.recent_events(session_id, USER_ID, limit=1000)
        if event["event_type"] in {"USER_MESSAGE", "ASSISTANT_MESSAGE", "MESSAGE"}
    ]


def _finished_checkpoint(session_id: str, messages: list[dict[str, str]]) -> AgentCheckpoint:
    return AgentCheckpoint(
        session_id=session_id,
        user_id=USER_ID,
        current_stage="FINISHED",
        status="finished",
        messages=messages,
    )


async def test_event_order_idempotency_and_conflicting_retry(mysql_store):
    store, session_id = mysql_store
    payload = {"name": "ticket_create", "result": {"ticket_id": "T-123", "details": [1, 2]}}
    first = await store.append_event(session_id, USER_ID, "tool_result", payload, event_key="tool:1")
    replay = await store.append_event(session_id, USER_ID, "tool_result", payload, event_key="tool:1")
    second = await store.append_event(session_id, USER_ID, "tool_call", {"name": "next"}, event_key="tool:2")

    assert (first["seq"], second["seq"]) == (1, 2)
    assert replay == first
    assert [event["seq"] for event in await store.recent_events(session_id, USER_ID)] == [1, 2]
    with pytest.raises(CheckpointConflict):
        await store.append_event(session_id, USER_ID, "tool_result", {"result": "changed"}, event_key="tool:1")
    assert await _event_count(store, session_id) == 2


async def test_concurrent_appends_allocate_monotonic_sequences(mysql_store):
    store, session_id = mysql_store
    events = await asyncio.gather(*(
        store.append_event(session_id, USER_ID, "tool_result", {"index": index}, event_key=f"parallel:{index}")
        for index in range(24)
    ))
    assert sorted(event["seq"] for event in events) == list(range(1, 25))
    assert len({event["event_id"] for event in events}) == 24
    ordered = await store.recent_events(session_id, USER_ID, limit=24)
    assert [event["seq"] for event in ordered] == list(range(1, 25))


async def test_parallel_calls_inside_session_lock_share_connection_safely(mysql_store):
    store, session_id = mysql_store
    async with store.session_lock(session_id):
        events = await asyncio.gather(*(
            store.append_event(
                session_id, USER_ID, "tool_result", {"index": index},
                event_key=f"leased-parallel:{index}",
            )
            for index in range(24)
        ))

    assert sorted(event["seq"] for event in events) == list(range(1, 25))
    assert len({event["event_id"] for event in events}) == 24
    ordered = await store.recent_events(session_id, USER_ID, limit=24)
    assert [event["seq"] for event in ordered] == list(range(1, 25))


async def test_checkpoint_read_and_write_use_digest_before_checkpoint_lock(mysql_store):
    store, session_id = mysql_store
    checkpoint = await store.save(AgentCheckpoint(
        session_id=session_id, user_id=USER_ID,
        messages=[{"role": "user", "content": "lock-order probe"}],
    ))
    operation = contextvars.ContextVar("checkpoint_lock_order_test_operation", default=None)
    reader_holds_checkpoint = threading.Event()
    release_reader = threading.Event()
    writer_attempted_digest = threading.Event()
    writer_acquired_digest = threading.Event()
    original_ensure_digest = store._ensure_digest
    original_hydrate = store._hydrate_checkpoint_tx

    def observe_ensure_digest(cursor, target_session, target_user):
        is_writer = operation.get() == "writer"
        if is_writer:
            writer_attempted_digest.set()
        result = original_ensure_digest(cursor, target_session, target_user)
        if is_writer:
            writer_acquired_digest.set()
        return result

    def pause_reader_after_checkpoint_lock(cursor, value):
        if operation.get() == "reader":
            reader_holds_checkpoint.set()
            if not release_reader.wait(timeout=10):
                raise TimeoutError("reader lock-order test barrier timed out")
            if writer_acquired_digest.is_set():
                raise RuntimeError("writer acquired digest while reader held checkpoint")
        return original_hydrate(cursor, value)

    async def read_checkpoint():
        token = operation.set("reader")
        try:
            return await store.load(session_id, USER_ID)
        finally:
            operation.reset(token)

    async def write_checkpoint():
        token = operation.set("writer")
        try:
            return await store.update(AgentCheckpoint.model_validate({
                **checkpoint.model_dump(), "intent": "writer update",
            }))
        finally:
            operation.reset(token)

    store._ensure_digest = observe_ensure_digest
    store._hydrate_checkpoint_tx = pause_reader_after_checkpoint_lock
    reader_task = None
    writer_task = None
    try:
        reader_task = asyncio.create_task(read_checkpoint())
        assert await asyncio.to_thread(reader_holds_checkpoint.wait, 5)
        writer_task = asyncio.create_task(write_checkpoint())
        assert await asyncio.to_thread(writer_attempted_digest.wait, 5)
        writer_got_digest_before_reader_release = await asyncio.to_thread(
            writer_acquired_digest.wait, 0.25
        )
        release_reader.set()
        results = await asyncio.gather(reader_task, writer_task, return_exceptions=True)
        assert writer_got_digest_before_reader_release is False
        assert all(not isinstance(result, BaseException) for result in results)
        assert results[0].session_id == session_id
        assert results[1].intent == "writer update"
    finally:
        release_reader.set()
        pending = [task for task in (reader_task, writer_task) if task is not None]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        store._ensure_digest = original_ensure_digest
        store._hydrate_checkpoint_tx = original_hydrate


async def test_missing_checkpoint_reads_do_not_create_owner_anchor(mysql_store):
    store, session_id = mysql_store
    assert await store.load(session_id, USER_ID) is None
    working_set = await store.load_working_set(session_id, USER_ID)
    assert working_set["checkpoint_version"] == 0
    digest_count = await store._call(
        lambda _connection, cursor: (
            cursor.execute("SELECT COUNT(*) AS count FROM session_digest WHERE session_id=%s", (session_id,)),
            int(cursor.fetchone()["count"]),
        )[1]
    )
    assert digest_count == 0

    await store.append_event(session_id, USER_ID, "tool_result", {"ok": True})
    assert await store.load(session_id, USER_ID) is None
    with pytest.raises(CheckpointOwnershipError):
        await store.load(session_id, "different-owner")


async def test_checkpoint_event_write_rolls_back_atomically(mysql_store):
    store, session_id = mysql_store
    checkpoint = AgentCheckpoint(
        session_id=session_id,
        user_id=USER_ID,
        messages=[{"role": "user", "content": "rollback me"}],
        context={"request_id": "rollback", "state": {}},
    )
    original = store._append_state_events_tx

    def fail_after_events(cursor, value):
        original(cursor, value)
        raise RuntimeError("injected failure after event append")

    store._append_state_events_tx = fail_after_events
    try:
        with pytest.raises(RuntimeError, match="injected failure"):
            await store.save(checkpoint)
    finally:
        store._append_state_events_tx = original

    assert await store.load(session_id, USER_ID) is None
    assert await store.history(session_id, USER_ID) == []
    assert await store.recent_events(session_id, USER_ID) == []
    assert await _event_count(store, session_id) == 0


async def test_checkpoint_message_count_does_not_load_full_history(mysql_store):
    store, session_id = mysql_store
    original_history = store._message_history_tx
    history_reads = []

    def observe_history(cursor, target_session, **kwargs):
        history_reads.append(target_session)
        return original_history(cursor, target_session, **kwargs)

    store._message_history_tx = observe_history
    messages = [
        {"role": "user", "content": "first question"},
        {"role": "assistant", "content": "first answer"},
    ]
    try:
        checkpoint = await store.save(AgentCheckpoint(
            session_id=session_id, user_id=USER_ID, messages=messages,
            context={"request_id": "counting-request"},
        ))
        await store.update(AgentCheckpoint.model_validate({
            **checkpoint.model_dump(),
            "messages": messages + [{"role": "user", "content": "next question"}],
        }))
    finally:
        store._message_history_tx = original_history

    assert history_reads == []
    assert await store.history(session_id, USER_ID) == messages + [
        {"role": "user", "content": "next question"},
    ]


async def test_identical_messages_in_new_requests_are_not_deduplicated(mysql_store):
    store, session_id = mysql_store
    pair = [
        {"role": "user", "content": "Where is order ORD-42?"},
        {"role": "assistant", "content": "It is ready for pickup."},
    ]
    first = await store.save(AgentCheckpoint(
        session_id=session_id, user_id=USER_ID, messages=pair,
        context={"request_id": "sidebar-request-1"},
    ))
    # A repeated checkpoint stage for the same request is still idempotent.
    same_request = await store.update(first)
    assert len(await _message_events(store, session_id)) == 2

    second_request = AgentCheckpoint.model_validate({
        **same_request.model_dump(), "context": {"request_id": "sidebar-request-2"},
    })
    await store.update(second_request)
    events = await _message_events(store, session_id)
    assert len(events) == 4
    assert [event["payload"] for event in events] == [
        {"role": "user", "content": pair[0]["content"]},
        {"role": "assistant", "content": pair[1]["content"]},
        {"role": "user", "content": pair[0]["content"]},
        {"role": "assistant", "content": pair[1]["content"]},
    ]


async def test_new_request_with_overlapping_history_appends_only_new_message(mysql_store):
    store, session_id = mysql_store
    old_history = [
        {"role": "user", "content": "previous question"},
        {"role": "assistant", "content": "previous answer"},
    ]
    checkpoint = await store.save(AgentCheckpoint(
        session_id=session_id, user_id=USER_ID, messages=old_history,
        context={"request_id": "overlap-request-1"},
    ))
    incoming = old_history + [{"role": "user", "content": "new question"}]
    await store.update(AgentCheckpoint.model_validate({
        **checkpoint.model_dump(), "messages": incoming,
        "context": {"request_id": "overlap-request-2"},
    }))

    events = await _message_events(store, session_id)
    assert [event["payload"] for event in events] == [
        {"role": item["role"], "content": item["content"]} for item in incoming
    ]


async def test_clear_cutoff_hides_old_history_summaries_but_never_deletes_events(mysql_store):
    store, session_id = mysql_store
    old_messages = [
        {"role": "user", "content": "private old request"},
        {"role": "assistant", "content": "private old answer"},
    ]
    checkpoint = await store.save(_finished_checkpoint(session_id, old_messages))
    await store.save_digest(
        session_id,
        USER_ID,
        rolling_summary="private old summary",
        archive_summary={"entities": ["private old entity"]},
        protected_fields={"order_id": "PRIVATE-OLD"},
        summary_event_seq=checkpoint.last_event_seq,
    )
    before_clear = await _event_count(store, session_id)
    await store.delete(session_id, USER_ID, checkpoint.version)

    assert await store.history(session_id, USER_ID) == []
    assert await store.recent_events(session_id, USER_ID) == []
    cold = await store.load_working_set(session_id, USER_ID)
    assert cold == {
        "session_id": session_id,
        "user_id": USER_ID,
        "recent_messages": [],
        "rolling_summary": None,
        "archive_summary": None,
        "session_state": {},
        "last_event_seq": before_clear + 1,
        "version": before_clear + 2,
        "checkpoint_version": 0,
        "protected_fields": {},
        "summary_event_seq": before_clear + 1,
        "cutoff_seq": before_clear + 1,
        "synchronized_request_id": None,
    }
    # Clear is a cutoff, not event deletion; the raw pre-clear records remain stored.
    assert await _event_count(store, session_id) == before_clear + 1

    # Reusing the old message/request shape after clear creates a fresh event, not an
    # idempotency collision with pre-clear content.
    new_checkpoint = await store.save(_finished_checkpoint(session_id, old_messages))
    assert new_checkpoint.last_event_seq > cold["last_event_seq"]
    assert await store.history(session_id, USER_ID) == old_messages
    raw_events = await store.recent_events(session_id, USER_ID, after_seq=cold["last_event_seq"] - 1)
    assert all(event["seq"] > cold["last_event_seq"] for event in raw_events)


async def test_legacy_snapshot_imports_once_and_preserves_only_available_messages(mysql_store):
    store, session_id = mysql_store
    legacy_messages = [
        {"role": "user", "content": "legacy user"},
        {"role": "assistant", "content": "legacy answer"},
    ]
    legacy = _finished_checkpoint(session_id, legacy_messages)
    raw_state = legacy.model_dump()
    raw_state.pop("last_event_seq")

    def insert_legacy(_connection, cursor):
        cursor.execute(
            """INSERT INTO agent_checkpoint
                (session_id,user_id,version,state_json,status,last_event_seq)
                VALUES (%s,%s,%s,%s,%s,0)""",
            (session_id, USER_ID, legacy.version, json.dumps(raw_state, ensure_ascii=False), legacy.status),
        )

    await store._call(insert_legacy)
    migrated = await store.load(session_id, USER_ID)
    assert [message.model_dump() for message in migrated.messages] == legacy_messages
    assert await store.history(session_id, USER_ID) == legacy_messages
    imported_count = await _event_count(store, session_id)
    assert imported_count == len(legacy_messages)

    again = await store.load(session_id, USER_ID)
    assert [message.model_dump() for message in again.messages] == legacy_messages
    assert await _event_count(store, session_id) == imported_count

    stored = await store._call(
        lambda _connection, cursor: (
            cursor.execute("SELECT state_json FROM agent_checkpoint WHERE session_id=%s", (session_id,)),
            cursor.fetchone()["state_json"],
        )[1]
    )
    if isinstance(stored, str):
        stored = json.loads(stored)
    assert stored["messages"] == []
    assert stored["last_event_seq"] == migrated.last_event_seq


async def test_raw_tool_results_remain_retrievable_across_summary_watermarks(mysql_store):
    store, session_id = mysql_store
    original_tool_result = {
        "tool": "order_lookup",
        "result": {"order_id": "ORD-ORIGINAL", "items": [{"sku": "SKU-1", "quantity": 3}]},
    }
    first = await store.append_event(session_id, USER_ID, "tool_result", original_tool_result, event_key="result:1")
    second = await store.append_event(
        session_id, USER_ID, "USER_MESSAGE", {"content": "next question"}, event_key="user:2"
    )
    await store.save_digest(
        session_id,
        USER_ID,
        rolling_summary="summary through tool result",
        archive_summary={"entities": ["ORD-ORIGINAL"]},
        protected_fields={"order_id": "ORD-ORIGINAL"},
        summary_event_seq=first["seq"],
    )

    # Event detail before the summary watermark stays available to direct readers;
    # callers may also continue from any explicit sequence cursor.
    events = await store.recent_events(session_id, USER_ID, after_seq=0)
    assert events[0]["payload"] == original_tool_result
    assert events[1]["seq"] == second["seq"]
    assert (await store.recent_events(session_id, USER_ID, after_seq=first["seq"]))[0]["payload"]["content"] == "next question"
    working_set = await store.load_working_set(session_id, USER_ID)
    assert working_set["session_id"] == session_id
    assert working_set["user_id"] == USER_ID
    assert working_set["archive_summary"] == {"entities": ["ORD-ORIGINAL"]}
    assert working_set["summary_event_seq"] == first["seq"]


async def test_event_detail_and_ascending_range_retrieve_old_raw_event(mysql_store):
    store, session_id = mysql_store
    original_tool_result = {
        "ticket": "T-ORIGINAL",
        "result": {"status": "ready", "items": ["part-1", "part-2"]},
    }
    last_seq = 1002

    def seed_events(connection, cursor):
        connection.begin()
        try:
            cursor.execute("""INSERT INTO session_digest
                (session_id,user_id,version,last_event_seq,summary_event_seq,cutoff_seq,protected_fields)
                VALUES (%s,%s,%s,%s,0,0,%s)""",
                           (session_id, USER_ID, last_seq, last_seq, "{}"))
            rows = []
            for seq in range(1, last_seq + 1):
                if seq == 1:
                    event_type = "tool_result"
                    payload = original_tool_result
                else:
                    event_type = "test_tick"
                    payload = {"index": seq}
                rows.append((session_id, USER_ID, seq, event_type, None, store._json_dump(payload)))
            cursor.executemany("""INSERT INTO conversation_event
                (session_id,user_id,seq,event_type,event_key,payload)
                VALUES (%s,%s,%s,%s,%s,%s)""", rows)
            cursor.execute("SELECT event_id FROM conversation_event WHERE session_id=%s AND seq=1", (session_id,))
            event_id = int(cursor.fetchone()["event_id"])
            connection.commit()
            return event_id
        except BaseException:
            connection.rollback()
            raise

    old_event_id = await store._call(seed_events)
    with pytest.raises(CheckpointOwnershipError):
        await store.get_event(session_id, "another-owner", old_event_id)

    old_event = await store.get_event(session_id, USER_ID, old_event_id)
    assert old_event["seq"] == 1
    assert old_event["event_type"] == "tool_result"
    assert old_event["payload"] == original_tool_result
    assert all(event["seq"] > 1 for event in await store.recent_events(session_id, USER_ID))

    all_events = []
    after_seq = 0
    while True:
        page = await store.events_range(session_id, USER_ID, after_seq=after_seq, limit=1000)
        if not page:
            break
        assert page[0]["seq"] == after_seq + 1
        all_events.extend(page)
        after_seq = page[-1]["seq"]
    assert [event["seq"] for event in all_events] == list(range(1, last_seq + 1))
    assert all_events[0]["payload"] == original_tool_result
    bounded = await store.events_range(session_id, USER_ID, after_seq=999, before_seq=1001, limit=10)
    assert [event["seq"] for event in bounded] == [1000, 1001]

    checkpoint = await store.save(_finished_checkpoint(session_id, []))
    await store.delete(session_id, USER_ID, checkpoint.version)
    assert await store.get_event(session_id, USER_ID, old_event_id) is None
    assert await store.events_range(session_id, USER_ID, after_seq=0, limit=1000) == []
    assert await _event_count(store, session_id) == last_seq + 1


async def test_owner_cas_running_delete_conflict_and_owner_anchor_survives_clear(mysql_store):
    store, session_id = mysql_store
    first = await store.save(AgentCheckpoint(session_id=session_id, user_id=USER_ID))
    second = await store.update(first)
    with pytest.raises(CheckpointConflict):
        await store.update(first)
    with pytest.raises(CheckpointConflict, match="unfinished"):
        await store.delete(session_id, USER_ID, second.version)

    finished = AgentCheckpoint.model_validate(
        {**second.model_dump(), "current_stage": "FINISHED", "status": "finished", "context": {}}
    )
    terminal = await store.update(finished)
    await store.delete(session_id, USER_ID, terminal.version)
    with pytest.raises(CheckpointOwnershipError):
        await store.append_event(session_id, "different-owner", "tool_result", {"ok": True})


async def test_summary_cursor_is_monotonic_bounded_and_unset_fields_are_preserved(mysql_store):
    store, session_id = mysql_store
    one = await store.append_event(session_id, USER_ID, "tool_result", {"value": 1}, event_key="one")
    two = await store.append_event(session_id, USER_ID, "tool_result", {"value": 2}, event_key="two")
    await store.save_digest(
        session_id,
        USER_ID,
        rolling_summary="first summary",
        archive_summary={"n": 1},
        protected_fields={"id": "KEEP"},
        summary_event_seq=one["seq"],
    )
    await store.save_digest(session_id, USER_ID, protected_fields={"id": "UPDATED"})
    preserved = await store.load_working_set(session_id, USER_ID)
    assert preserved["rolling_summary"] == "first summary"
    assert preserved["archive_summary"] == {"n": 1}
    assert preserved["summary_event_seq"] == one["seq"]

    await store.save_digest(session_id, USER_ID, summary_event_seq=two["seq"])
    with pytest.raises(CheckpointConflict, match="watermark"):
        await store.save_digest(session_id, USER_ID, summary_event_seq=one["seq"])
    with pytest.raises(CheckpointConflict, match="watermark"):
        await store.save_digest(session_id, USER_ID, summary_event_seq=two["seq"] + 1)
    with pytest.raises(CheckpointConflict, match="explicit event watermark"):
        await store.save_digest(session_id, USER_ID, rolling_summary="unattributed")


@pytest.mark.asyncio
async def test_real_redis_short_ttl_expiry_restores_from_mysql_and_rewarms(mysql_store):
    """Real-Redis TTL expiry -> MySQL cold restore -> Redis rewarm, in seconds not 1800s."""
    if os.getenv("SMARTCS_CONTEXT_REDIS_TEST") != "1":
        pytest.skip("real Redis disabled; set SMARTCS_CONTEXT_REDIS_TEST=1 explicitly")
    from context.manager import ContextManager, WorkingSetCache
    from context.models import ModelProfile
    from langchain_core.messages import HumanMessage
    from memory.short_term import ShortTermMemory

    store, session_id = mysql_store
    user_id = USER_ID
    text = "Short TTL real Redis restore fixture turn."
    await store.append_event(session_id, user_id, "USER_MESSAGE", {"role": "user", "content": text})
    await store.append_event(session_id, user_id, "ASSISTANT_MESSAGE", {"role": "assistant", "content": "short-ttl-ack"})

    # Default to the IPv4 loopback: this container publishes 127.0.0.1:6379 only.
    short = ShortTermMemory(
        redis_url=os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0"), ttl_seconds=1,
    )
    cache = WorkingSetCache(short, ttl=1)
    profile = ModelProfile(
        name="short-ttl-model", provider="test", context_limit=8192,
        max_output_tokens=256, reserve=128, safety_margin=128,
    )
    manager = ContextManager(event_store=store, cache=cache, default_model=profile)

    async def build_turn(request_id: str):
        state = {
            "messages": [HumanMessage(content=text)],
            "sub_results": {},
            "session_id": session_id,
            "user_id": user_id,
            "client_request_id": request_id,
        }
        async with manager.bind_request(session_id, user_id, request_id=request_id, state=state):
            return await manager.build(
                session_id, user_id, "conversation", text, profile, state=state,
                system_prompt="Short TTL real Redis integration test.",
            )

    try:
        first = await build_turn("short-ttl-1")
        assert first.diagnostics.get("working_set_cache_backend") == "redis"
        assert first.diagnostics.get("working_set_cache_is_real_redis") is True
        assert first.diagnostics.get("working_set_cache_hit") is False

        warm = await build_turn("short-ttl-2")
        assert warm.diagnostics.get("working_set_cache_hit") is True
        assert warm.diagnostics.get("working_set_cache_is_real_redis") is True

        await asyncio.sleep(1.2)  # real Redis key TTL expires; no 1800-second wait

        restored = await build_turn("short-ttl-3")
        assert restored.diagnostics.get("working_set_cache_hit") is False
        assert restored.diagnostics.get("storage_read_path") == "cold_event_store_restore"
        assert restored.diagnostics.get("working_set_cache_is_real_redis") is True
        assert "short-ttl-ack" in "".join(
            str(getattr(message, "content", "")) for message in restored.messages
        ), "cold restore lost durable recent history"

        rewarmed = await build_turn("short-ttl-4")
        assert rewarmed.diagnostics.get("working_set_cache_hit") is True
        assert rewarmed.diagnostics.get("working_set_cache_is_real_redis") is True
    finally:
        await cache.invalidate(session_id, user_id)
        redis_client = getattr(short, "_redis", None)
        if redis_client is not None:
            await redis_client.aclose()
