from __future__ import annotations

import asyncio
from copy import deepcopy
import json

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from context.invocation import invoke_agent
from context.manager import ContextManager, WorkingSetCache, active_context
from context.models import ContextOverflowError, ModelProfile
from memory.short_term import ShortTermMemory


def _byte_tokenizer(text: str) -> list[int]:
    """Deterministic test-only tokenizer fixture; not a production tokenizer."""
    return list(text.encode("utf-8"))


class FakeEventStore:
    def __init__(self) -> None:
        self.load_count = 0
        self.recent_count = 0
        self.range_count = 0
        self.digest_count = 0
        self.cutoff_seq = 0
        self.events: list[dict] = []
        self.event_keys: dict[str, dict] = {}
        self.working_set = {
            "session_id": "s-1",
            "user_id": "u-1",
            "recent_messages": [],
            "recent_tool_events": [],
            "rolling_summary": "",
            "archive_summary": {},
            "session_state": {},
            "last_event_seq": 0,
            "version": 0,
            "checkpoint_version": 1,
            "cutoff_seq": 0,
            "protected_fields": {},
            "summary_event_seq": 0,
        }

    async def load_working_set(self, session_id: str, user_id: str, *, recent_limit: int = 8) -> dict:
        self.load_count += 1
        result = deepcopy(self.working_set)
        result["session_id"] = session_id
        result["user_id"] = user_id
        result["recent_messages"] = result["recent_messages"][-recent_limit:]
        result["cutoff_seq"] = self.cutoff_seq
        return result

    async def recent_events(self, session_id: str, user_id: str, *, after_seq: int = 0, limit: int = 100) -> list[dict]:
        self.recent_count += 1
        visible = [event for event in self.events if int(event["seq"]) > self.cutoff_seq]
        return deepcopy(visible[-limit:])

    async def events_range(
        self,
        session_id: str,
        user_id: str,
        *,
        after_seq: int = 0,
        before_seq: int | None = None,
        limit: int = 1000,
    ) -> list[dict]:
        self.range_count += 1
        before = before_seq if before_seq is not None else float("inf")
        return deepcopy([
            event for event in self.events
            if max(after_seq, self.cutoff_seq) < event["seq"] <= before
        ][:limit])

    async def append_event(self, session_id: str, user_id: str, event_type: str, payload: dict, *, event_key: str | None = None) -> dict:
        if event_key in self.event_keys:
            return deepcopy(self.event_keys[event_key])
        event = {
            "seq": len(self.events) + 1,
            "event_id": len(self.events) + 100,
            "event_key": event_key,
            "event_type": event_type,
            "payload": deepcopy(payload),
        }
        self.events.append(event)
        self.event_keys[event_key] = deepcopy(event)
        self.working_set["last_event_seq"] = event["seq"]
        return deepcopy(event)

    async def save_digest(self, session_id: str, user_id: str, **kwargs) -> None:
        self.digest_count += 1
        if kwargs.get("rolling_summary") is not None:
            self.working_set["rolling_summary"] = kwargs["rolling_summary"]
        if kwargs.get("archive_summary") is not None:
            self.working_set["archive_summary"] = deepcopy(kwargs["archive_summary"])
        if kwargs.get("protected_fields") is not None:
            self.working_set["protected_fields"] = deepcopy(kwargs["protected_fields"])
        self.working_set["summary_event_seq"] = kwargs.get("summary_event_seq", 0)
        self.working_set["version"] += 1


class ResurrectingCache:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], dict] = {}
        self.fail_invalidate = False
        self.fail_put = False
        self.last_attempted_put: dict | None = None

    @property
    def backend_status(self) -> dict[str, object]:
        return {"backend": "test_redis", "redis_available": True}

    async def get(self, session_id: str, user_id: str) -> dict | None:
        value = self.values.get((session_id, user_id))
        return deepcopy(value) if value is not None else None

    async def put(self, session_id: str, user_id: str, working_set: dict) -> None:
        self.last_attempted_put = deepcopy(working_set)
        if self.fail_put:
            raise OSError("simulated Redis write failure")
        self.values[(session_id, user_id)] = deepcopy(working_set)

    async def invalidate(self, session_id: str, user_id: str) -> None:
        if self.fail_invalidate:
            raise OSError("simulated Redis delete failure")
        self.values.pop((session_id, user_id), None)


class RecordingLLM:
    def __init__(self, response: str = "ok") -> None:
        self.calls = 0
        self.messages: list = []
        self.response = response

    async def ainvoke(self, messages):
        self.calls += 1
        self.messages = messages
        return AIMessage(content=self.response)


class TypeErrorLLM:
    def __init__(self) -> None:
        self.calls = 0

    async def ainvoke(self, messages):
        self.calls += 1
        raise TypeError("raised inside the real model invocation")


def _profile() -> ModelProfile:
    return ModelProfile(
        name="test-model",
        provider="test",
        context_limit=8192,
        max_output_tokens=256,
        reserve=128,
        safety_margin=128,
        hard_ratio=0.9,
        soft_ratio=0.7,
        recent_messages=8,
        compression_attempts=12,
    )


def _memory_cache(ttl: int = 1800) -> WorkingSetCache:
    return WorkingSetCache(ShortTermMemory(ttl_seconds=ttl), ttl=ttl)


@pytest.mark.asyncio
async def test_hot_cache_uses_trusted_checkpoint_without_mysql_reads() -> None:
    store = FakeEventStore()
    cache = _memory_cache()
    await cache.put("s-1", "u-1", {
        **deepcopy(store.working_set),
        "last_event_seq": 6,
        "checkpoint_version": 3,
    })
    manager = ContextManager(event_store=store, cache=cache, tokenizer=_byte_tokenizer, default_model=_profile())
    state = {"session_id": "s-1", "user_id": "u-1", "_checkpoint_last_event_seq": 6, "_checkpoint_version": 3}

    package = await manager.build("s-1", "u-1", "conversation", "hello", state=state)

    assert store.load_count == 0
    assert store.recent_count == 0
    assert package.diagnostics["storage_read_path"] == "owner_scoped_cache"
    assert package.diagnostics["working_set_cache_is_real_redis"] is False
    assert package.total_tokens <= _profile().prompt_budget


@pytest.mark.asyncio
async def test_stale_cache_restores_once_then_uses_hot_path() -> None:
    store = FakeEventStore()
    store.working_set["last_event_seq"] = 8
    store.working_set["checkpoint_version"] = 4
    cache = _memory_cache()
    await cache.put("s-1", "u-1", {
        **deepcopy(store.working_set),
        "last_event_seq": 2,
        "checkpoint_version": 2,
    })
    manager = ContextManager(event_store=store, cache=cache, tokenizer=_byte_tokenizer, default_model=_profile())
    state = {"session_id": "s-1", "user_id": "u-1"}

    async with manager.bind_request("s-1", "u-1", "req-1", state, last_event_seq=8, checkpoint_version=4):
        first = await manager.build("s-1", "u-1", "conversation", "hello", state=state)
        second = await manager.build("s-1", "u-1", "conversation", "again", state=state)

    assert store.load_count == 1
    assert first.diagnostics["storage_read_path"] == "cold_event_store_restore"
    assert second.diagnostics["storage_read_path"] == "owner_scoped_cache"


@pytest.mark.asyncio
async def test_new_manager_restores_durable_context_after_owned_cache_loss() -> None:
    store = FakeEventStore()
    store.working_set.update({
        "last_event_seq": 4,
        "checkpoint_version": 2,
        "recent_messages": [{"role": "user", "content": "durable prior question"}],
        "rolling_summary": "[seq=1; event_id=100; type=USER_MESSAGE] user: durable fact",
        "archive_summary": {"provenance": [{"seq": 1, "event_id": 100, "event_type": "USER_MESSAGE"}]},
        "summary_event_seq": 4,
    })
    cache = _memory_cache()
    first_manager = ContextManager(event_store=store, cache=cache, tokenizer=_byte_tokenizer, default_model=_profile())
    await first_manager.build("s-1", "u-1", "conversation", "current question")
    await first_manager.invalidate_session("s-1", "u-1")

    restarted = ContextManager(event_store=store, cache=cache, tokenizer=_byte_tokenizer, default_model=_profile())
    package = await restarted.build("s-1", "u-1", "conversation", "current question")
    prompt = "\n".join(str(message.content) for message in package.messages)

    assert store.load_count == 2
    assert package.diagnostics["storage_read_path"] == "cold_event_store_restore"
    assert "durable fact" in prompt
    assert "durable prior question" in prompt
    assert package.total_tokens <= _profile().prompt_budget


@pytest.mark.asyncio
async def test_clear_cutoff_and_version_reset_cannot_resurrect_stale_cache_generation() -> None:
    store = FakeEventStore()
    cache = ResurrectingCache()
    session_id, user_id = "s-1", "u-1"
    reused_text = "同一请求文本"
    old_pending = {
        "type": "refund_create", "order_id": "ORD-PRIVATE-OLD", "amount": 88.75,
        "idempotency_key": "refund:private-old", "arguments": {"order_id": "ORD-PRIVATE-OLD"},
    }
    store.events = [
        {"seq": 1, "event_id": 101, "event_type": "USER_MESSAGE", "payload": {"role": "user", "content": "private old request"}},
        {"seq": 2, "event_id": 102, "event_type": "ASSISTANT_MESSAGE", "payload": {"role": "assistant", "content": "private old answer"}},
        {"seq": 3, "event_id": 103, "event_type": "TOOL_CALL", "payload": {"tool_name": "refund.create", "arguments": old_pending["arguments"], "pending_action": old_pending}},
        {"seq": 4, "event_id": 104, "event_type": "STATE_CHANGE", "payload": {"pending_action": old_pending}},
    ]
    stale = {
        **deepcopy(store.working_set),
        "recent_messages": [{"role": "user", "content": "private old request"}, {"role": "assistant", "content": "private old answer"}],
        "recent_tool_events": [store.events[2], store.events[3]],
        "rolling_summary": "PRIVATE OLD SUMMARY",
        "archive_summary": {"provenance": [{"seq": 4, "event_id": 104}], "facts": ["PRIVATE OLD FACT"]},
        "session_state": {"pending_action": old_pending, "last_intent": "refund_handler"},
        "last_event_seq": 4,
        "checkpoint_version": 8,
        "cutoff_seq": 0,
        "protected_fields": {"pending_action": old_pending, "arguments": old_pending["arguments"]},
        "summary_event_seq": 4,
    }
    cache.values[(session_id, user_id)] = deepcopy(stale)
    cache.fail_invalidate = True
    cache.fail_put = True
    manager = ContextManager(event_store=store, cache=cache, tokenizer=_byte_tokenizer, default_model=_profile())
    with pytest.raises(OSError, match="delete failure"):
        await manager.invalidate_session(session_id, user_id)

    # The durable clear is a cutoff. Raw event rows remain, but the authoritative
    # working set and new checkpoint belong to a new generation.
    store.cutoff_seq = 5
    store.events.append({"seq": 5, "event_id": 105, "event_type": "HISTORY_CLEARED", "payload": {}})
    store.events.append({"seq": 6, "event_id": 106, "event_type": "USER_MESSAGE", "payload": {"role": "user", "content": reused_text}})
    cleared_state = {"last_intent": "conversation", "turn_count": 1, "pending_action": None, "accumulated_entities": {}}
    store.working_set = {
        "session_id": session_id, "user_id": user_id,
        "recent_messages": [{"role": "user", "content": reused_text}],
        "recent_tool_events": [], "rolling_summary": "", "archive_summary": {},
        "session_state": cleared_state, "last_event_seq": 6, "version": 12,
        "checkpoint_version": 1, "cutoff_seq": 5,
        "protected_fields": {"pending_action": None}, "summary_event_seq": 5,
    }
    new_checkpoint = {
        "session_id": session_id, "user_id": user_id, "version": 1, "last_event_seq": 6,
        "pending_action": None, "messages": [{"role": "user", "content": reused_text}],
        "context": {"request_id": "same-client-request", "session_state": cleared_state},
    }
    current_state = {"session_id": session_id, "user_id": user_id, "session_state": cleared_state,
                    "messages": [HumanMessage(content=reused_text)]}
    async with manager.bind_request(session_id, user_id, "same-client-request", current_state,
                                    last_event_seq=6, checkpoint_version=1) as request:
        synchronized = await manager.synchronize_checkpoint(new_checkpoint)
        assert synchronized is False
        assert request.trusted_last_event_seq == 6
    attempted = cache.last_attempted_put
    assert attempted is not None and attempted["requires_cold_restore"] is True
    assert attempted["recent_tool_events"] == []
    assert attempted["rolling_summary"] == ""
    assert attempted["archive_summary"] == {}
    assert "PRIVATE OLD SUMMARY" not in json.dumps(attempted)
    assert "ORD-PRIVATE-OLD" not in json.dumps(attempted)

    # Redis becomes readable with its undeleted old payload. A fresh manager must
    # distrust its pre-cutoff cursor and cold-rebuild before assembling a prompt.
    cache.fail_put = False
    restarted = ContextManager(event_store=store, cache=cache, tokenizer=_byte_tokenizer, default_model=_profile())
    business_writes = 0
    async with restarted.bind_request(session_id, user_id, "same-client-request", current_state,
                                      last_event_seq=6, checkpoint_version=1):
        package = await restarted.build(session_id, user_id, "conversation", reused_text, state=current_state)
    prompt = "\n".join(str(message.content) for message in package.messages)
    assert package.diagnostics["storage_read_path"] == "cold_event_store_restore"
    assert store.load_count == 1
    assert "private old request" not in prompt
    assert "private old answer" not in prompt
    assert "PRIVATE OLD SUMMARY" not in prompt
    assert "ORD-PRIVATE-OLD" not in prompt
    assert "refund:private-old" not in prompt
    assert package.protected_fields == {"pending_action": None}
    assert business_writes == 0
    assert len(store.events) == 6
    assert store.events[0]["payload"]["content"] == "private old request"
    refreshed = cache.values[(session_id, user_id)]
    assert refreshed["last_event_seq"] == 6
    assert refreshed["cutoff_seq"] == 5
    assert refreshed["recent_tool_events"] == []


@pytest.mark.asyncio
async def test_summary_cursor_advances_only_through_contiguous_range_pages() -> None:
    store = FakeEventStore()
    store.events = [
        {
            "seq": seq,
            "event_id": seq + 100,
            "event_type": "USER_MESSAGE",
            "payload": {"text": f"event {seq}"},
        }
        for seq in range(1, 1201)
    ]
    store.working_set["last_event_seq"] = 1200
    manager = ContextManager(event_store=store, cache=_memory_cache(), tokenizer=_byte_tokenizer, default_model=_profile())

    for expected_cursor in (500, 1000, 1200):
        await manager.build("s-1", "u-1", "conversation", "current request")
        assert store.working_set["summary_event_seq"] == expected_cursor

    assert store.range_count == 3
    assert store.digest_count == 3
    assert len(store.events) == 1200
    assert len(store.working_set["archive_summary"]["provenance"]) == 1200
    assert len(store.working_set["rolling_summary"].splitlines()) <= 12
    assert len(store.working_set["rolling_summary"]) <= 2400
    assert manager._last_diagnostics["block_tokens"]["Summary"] <= 620


@pytest.mark.asyncio
async def test_nested_bind_and_invoke_reuse_request_context_and_preserve_state() -> None:
    manager = ContextManager(tokenizer=_byte_tokenizer, default_model=_profile())
    llm = RecordingLLM()
    original_messages = [HumanMessage(content="old question"), AIMessage(content="old answer"), HumanMessage(content="current question")]
    state = {
        "session_id": "s-1",
        "user_id": "u-1",
        "messages": original_messages,
        "sub_results": {"tool": {"raw_output": "must not become required state"}},
        "pending_action": {"type": "refund_create", "order_id": "ORD-1", "amount": "12.00"},
    }

    async with manager.bind_request("s-1", "u-1", "req-stable", state) as outer:
        await manager.invoke(
            llm,
            "refund_handler",
            [SystemMessage(content="preserve this exact system prefix"), HumanMessage(content="agent task")],
            state=state,
            session_id="s-1",
            user_id="u-1",
            task_message="final task text",
        )
        assert active_context.get() is outer
        assert outer.owner == ("s-1", "u-1")
        assert outer.request_id == "req-stable"
        assert state["messages"] is original_messages
        prompt = "\n".join(str(message.content) for message in llm.messages)
        assert "preserve this exact system prefix" in prompt
        assert "old question" in prompt and "current question" in prompt
        assert "agent task" not in prompt  # task_message supersedes the supplied latest task only
        assert "final task text" in prompt
        assert "raw_output" not in prompt
        assert "ORD-1" in prompt and "12.00" in prompt

    assert active_context.get() is None
    assert llm.calls == 1


@pytest.mark.asyncio
async def test_cancel_tombstones_stale_pending_action_from_p1_but_preserves_live_fields() -> None:
    store = FakeEventStore()
    cache = _memory_cache()
    manager = ContextManager(event_store=store, cache=cache, tokenizer=_byte_tokenizer, default_model=_profile())
    pending = {
        "type": "refund_create",
        "order_id": "ORD-PENDING-OLD-7",
        "amount": 107.25,
        "idempotency_key": "refund:old-7",
        "arguments": {"order_id": "ORD-PENDING-OLD-7", "reason": "fixture"},
    }
    prep_state = {
        "last_intent": "refund_handler",
        "turn_count": 1,
        "pending_action": pending,
        "accumulated_entities": {"order_id": "ORD-PENDING-OLD-7"},
    }
    store.events = [{"seq": 1, "event_id": 101, "event_type": "USER_MESSAGE", "payload": {"role": "user", "content": "申请退款"}}]
    store.working_set.update({
        "session_state": prep_state,
        "last_event_seq": 1,
        "checkpoint_version": 1,
        "recent_messages": [{"role": "user", "content": "申请退款"}],
    })
    prepare = {
        "session_id": "s-1", "user_id": "u-1", "version": 1, "last_event_seq": 1,
        "pending_action": pending, "messages": [{"role": "user", "content": "申请退款"}],
        "context": {"request_id": "prepare", "session_state": prep_state},
    }
    await manager.synchronize_checkpoint(prepare)
    async with manager.bind_request("s-1", "u-1", "prepare", state={"session_state": prep_state}, last_event_seq=1, checkpoint_version=1):
        prepared = await manager.build("s-1", "u-1", "refund_handler", "确认退款", state={"session_state": prep_state})
    assert prepared.protected_fields["pending_action"]["order_id"] == "ORD-PENDING-OLD-7"
    assert prepared.protected_fields["pending_action"]["amount"] == 107.25
    assert prepared.protected_fields["pending_action"]["arguments"]["order_id"] == "ORD-PENDING-OLD-7"
    assert prepared.protected_fields["accumulated_entities"]["order_id"] == "ORD-PENDING-OLD-7"

    old_tool_event = {
        "seq": 2,
        "event_id": 102,
        "event_type": "TOOL_CALL",
        "payload": {"tool_name": "refund.create", "arguments": pending["arguments"], "pending_action": pending},
    }
    store.events.append(old_tool_event)
    store.working_set.update({"last_event_seq": 2, "recent_tool_events": [old_tool_event]})
    cached = await cache.get("s-1", "u-1")
    assert cached is not None
    cached["last_event_seq"] = 2
    cached["recent_tool_events"] = [old_tool_event]
    cached["protected_fields"] = {"pending_action": pending, "arguments": pending["arguments"]}
    await cache.put("s-1", "u-1", cached)

    cancel_state = {"last_intent": "cancel", "turn_count": 2, "pending_action": None, "accumulated_entities": {}}
    cancel = {
        "session_id": "s-1", "user_id": "u-1", "version": 2, "last_event_seq": 3,
        "pending_action": None, "messages": [{"role": "user", "content": "取消退款"}],
        "context": {"request_id": "cancel", "session_state": cancel_state},
    }
    await manager.synchronize_checkpoint(cancel)

    business_writes = 0
    natural_state = {"session_state": {**cancel_state, "last_intent": "conversation", "turn_count": 3},
                     "messages": [HumanMessage(content="请问营业时间？")]}
    canceled = await manager.build("s-1", "u-1", "conversation", "请问营业时间？", state=natural_state)
    prompt = str(canceled.messages[-1].content)
    p1 = "\n".join(
        prompt.split(f"<{name}>\n", 1)[1].split(f"\n</{name}>", 1)[0]
        for name in ("SessionState", "ProtectedFields")
    )

    assert "pending_action" in p1 and "null" in p1
    assert "ORD-PENDING-OLD-7" not in p1
    assert "refund:old-7" not in p1
    assert "arguments" not in p1
    assert "107.25" not in p1
    assert canceled.protected_fields == {"pending_action": None}
    assert business_writes == 0


@pytest.mark.asyncio
async def test_fifty_warm_checkpoint_syncs_preserve_recent_message_pairs_and_cold_parity() -> None:
    store = FakeEventStore()
    cache = _memory_cache()
    manager = ContextManager(event_store=store, cache=cache, tokenizer=_byte_tokenizer, default_model=_profile())
    all_messages: list[dict[str, str]] = []
    state = {"last_intent": "conversation", "turn_count": 50, "pending_action": None}

    for turn in range(1, 51):
        pair = [
            {"role": "user", "content": "same repeated user text"},
            {"role": "assistant", "content": "same repeated assistant text"},
        ]
        all_messages.extend(pair)
        checkpoint = {
            "session_id": "s-1", "user_id": "u-1", "version": turn,
            "last_event_seq": turn * 2, "pending_action": None,
            "messages": pair,
            "context": {"request_id": f"turn-{turn}", "session_state": state},
        }
        await manager.synchronize_checkpoint(checkpoint)
        # Same-request retry/stage synchronization must be idempotent.
        await manager.synchronize_checkpoint(checkpoint)

    cached = await cache.get("s-1", "u-1")
    assert cached is not None
    expected_recent = all_messages[-20:]
    assert cached["recent_messages"] == expected_recent
    assert sum(message["role"] == "user" for message in expected_recent) == 10
    assert sum(message["role"] == "assistant" for message in expected_recent) == 10

    store.working_set.update({
        "recent_messages": expected_recent,
        "synchronized_request_id": "turn-50",
        "session_state": state,
        "last_event_seq": 100,
        "checkpoint_version": 50,
        "summary_event_seq": 100,
        "cutoff_seq": 0,
    })
    request_state = {"session_id": "s-1", "user_id": "u-1", "session_state": state,
                     "messages": [HumanMessage(content="same repeated user text")]}
    async with manager.bind_request("s-1", "u-1", "turn-50", request_state, last_event_seq=100, checkpoint_version=50):
        hot_package = await manager.build("s-1", "u-1", "conversation", "same repeated user text", state=request_state)
    await manager.invalidate_session("s-1", "u-1")
    restarted = ContextManager(event_store=store, cache=cache, tokenizer=_byte_tokenizer, default_model=_profile())
    async with restarted.bind_request("s-1", "u-1", "turn-50", request_state, last_event_seq=100, checkpoint_version=50):
        cold_package = await restarted.build("s-1", "u-1", "conversation", "same repeated user text", state=request_state)

    assert hot_package.diagnostics["storage_read_path"] == "owner_scoped_cache"
    assert cold_package.diagnostics["storage_read_path"] == "cold_event_store_restore"
    assert [message.content for message in hot_package.messages] == [message.content for message in cold_package.messages]
    prompt = str(hot_package.messages[-1].content)
    history_text = prompt.split("<RecentHistory>\n", 1)[1].split("\n</RecentHistory>", 1)[0]
    visible_history = json.loads(history_text)
    assert len(visible_history) == 7
    assert visible_history[-1]["role"] == "assistant"
    assert visible_history.count({"role": "user", "content": "same repeated user text"}) >= 3


@pytest.mark.asyncio
async def test_current_only_state_is_supplemented_from_working_set_without_deduping() -> None:
    store = FakeEventStore()
    store.working_set["recent_messages"] = [
        {"role": "user", "content": "确认"},
        {"role": "assistant", "content": "第一次确认收到"},
        {"role": "user", "content": "确认"},
        {"role": "assistant", "content": "继续处理"},
    ]
    manager = ContextManager(event_store=store, tokenizer=_byte_tokenizer, default_model=_profile())
    package = await manager.build(
        "s-1", "u-1", "conversation", "确认",
        state={"messages": [HumanMessage(content="确认")]},
    )
    human_prompt = str(package.messages[-1].content)

    assert "<RecentHistory>" in human_prompt
    assert "第一次确认收到" in human_prompt
    assert "继续处理" in human_prompt
    assert human_prompt.count('"content": "确认"') == 2
    assert human_prompt.count("<CurrentUser>") == 1
    history_text = human_prompt.split("<RecentHistory>\n", 1)[1].split("\n</RecentHistory>", 1)[0]
    assert len(json.loads(history_text)) == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("agent", ["knowledge_rag.rewrite", "compliance_checker"])
async def test_isolated_query_calls_skip_durable_state_lookups(agent: str) -> None:
    store = FakeEventStore()
    manager = ContextManager(event_store=store, tokenizer=_byte_tokenizer, default_model=_profile())
    llm = RecordingLLM()
    state = {
        "session_id": "s-1",
        "user_id": "u-1",
        "session_state": {"pending_action": {"order_id": "PRIVATE-ORDER-1"}},
    }

    async with manager.bind_request("s-1", "u-1", "outer-request", state) as request:
        response = await invoke_agent(
            llm,
            agent,
            [HumanMessage(content="query-only input")],
            state=state,
            isolated=True,
        )
        assert active_context.get() is request

    rendered = "\n".join(str(message.content) for message in llm.messages)
    package = await manager.build("s-1", "u-1", agent, "query-only input", state=state)
    assert response.content == "ok"
    assert "query-only input" in rendered
    assert "PRIVATE-ORDER-1" not in rendered
    assert package.protected_fields == {}
    assert store.load_count == 0
    assert request.request_id == "outer-request"


@pytest.mark.asyncio
async def test_memory_block_physical_order_does_not_follow_priority_order() -> None:
    class UserMemory:
        async def profile_cards(self, user_id: str, limit: int = 3):
            return [{"type": "profile", "fact": "preferred language"}]

        async def retrieve(self, user_id: str, query: str, top_k: int = 3):
            return [{"type": "episodic", "hint": "prior issue"}]

    manager = ContextManager(user_memory=UserMemory(), tokenizer=_byte_tokenizer, default_model=_profile())
    package = await manager.build(
        "s-1", "u-1", "conversation", "hello",
        state={"session_state": {"turn_count": 4}},
    )
    human_prompt = str(package.messages[-1].content)

    assert human_prompt.index("<UserProfileCards>") < human_prompt.index("<RetrievedUserMemory>")
    assert human_prompt.index("<RetrievedUserMemory>") < human_prompt.index("<SessionState>")
    assert package.block_tokens["UserProfileCards"] > 0
    assert package.block_tokens["RetrievedUserMemory"] > 0


@pytest.mark.asyncio
async def test_metrics_snapshot_exposes_numeric_block_and_memory_counters_without_content() -> None:
    class UserMemory:
        async def profile_cards(self, user_id: str, limit: int = 3):
            return [{"fact": "PRIVATE PROFILE A"}, {"fact": "PRIVATE PROFILE B"}]

        async def retrieve(self, user_id: str, query: str, top_k: int = 3):
            return [{"hint": "PRIVATE RETRIEVED A"}, {"hint": "PRIVATE RETRIEVED B"}]

    manager = ContextManager(user_memory=UserMemory(), tokenizer=_byte_tokenizer, default_model=_profile())
    package = await manager.build("PRIVATE-SESSION", "PRIVATE-OWNER", "conversation", "PRIVATE-PROMPT")
    metrics = manager.metrics_snapshot()

    assert metrics["context_user_profile_reads_total"] == 1
    assert metrics["context_user_profile_cards_total"] == 2
    assert metrics["context_retrieved_memory_queries_total"] == 1
    assert metrics["context_retrieved_memory_hit_queries_total"] == 1
    assert metrics["context_retrieved_memory_hits_total"] == 2
    assert metrics["context_block_user_profile_cards_tokens_last"] == package.block_tokens["UserProfileCards"]
    assert metrics["context_block_retrieved_user_memory_tokens_last"] == package.block_tokens["RetrievedUserMemory"]
    assert metrics["context_block_evidence_tokens_last"] == 0
    assert metrics["context_block_summary_ratio_last"] == 0.0
    assert metrics["context_block_tokens_total_last"] == package.total_tokens
    assert all(type(value) in (int, float) for value in metrics.values())
    serialized = json.dumps(metrics)
    for private_value in ("PRIVATE-SESSION", "PRIVATE-OWNER", "PRIVATE-PROMPT", "PRIVATE PROFILE", "PRIVATE RETRIEVED"):
        assert private_value not in serialized


@pytest.mark.asyncio
async def test_tight_budget_drops_episodic_memory_before_current_evidence() -> None:
    class UserMemory:
        async def profile_cards(self, user_id: str, limit: int = 3):
            return [{"type": "profile", "fact": "preferred contact: email"}]

        async def retrieve(self, user_id: str, query: str, top_k: int = 3):
            return [{"type": "episodic", "hint": "old ticket summary " * 90}]

    profile = ModelProfile(
        name="test-tight",
        provider="test",
        context_limit=1800,
        max_output_tokens=200,
        reserve=100,
        safety_margin=100,
        hard_ratio=0.9,
        soft_ratio=0.7,
        compression_attempts=12,
    )
    manager = ContextManager(user_memory=UserMemory(), tokenizer=_byte_tokenizer, default_model=profile)
    package = await manager.build(
        "s-1",
        "u-1",
        "knowledge_rag",
        "current ticket status?",
        evidence=[{"source": "live-tool", "content": "CURRENT_TOOL_TRUTH: the ticket is open"}],
    )

    prompt = "\n".join(str(message.content) for message in package.messages)
    assert "CURRENT_TOOL_TRUTH" in prompt
    assert "old ticket summary" not in prompt
    assert "RetrievedUserMemory" in package.diagnostics["dropped_blocks"]
    assert "Evidence" in package.block_tokens
    assert "UserProfileCards" in package.block_tokens
    assert package.block_tokens["Evidence"] > 0


@pytest.mark.asyncio
async def test_rewrite_policy_has_only_query_and_no_memory_history_or_status() -> None:
    manager = ContextManager(tokenizer=_byte_tokenizer, default_model=_profile())
    package = await manager.build(
        "s-1",
        "u-1",
        "knowledge_rag.rewrite",
        "查询 AppleCare+ 保修范围",
        state={"messages": [HumanMessage(content="old user text"), AIMessage(content="old assistant text")]},
        system_prompt="rewrite exactly",
    )

    prompt = "\n".join(str(message.content) for message in package.messages)
    assert "rewrite exactly" in prompt
    assert "查询 AppleCare+ 保修范围" in prompt
    assert "old user text" not in prompt
    assert "old assistant text" not in prompt
    assert "<StatusBar>" not in prompt
    assert len(package.messages) == 2  # stable instruction prefix + query-only dynamic block


@pytest.mark.asyncio
async def test_typeerror_from_invocation_is_not_retried() -> None:
    manager = ContextManager(tokenizer=_byte_tokenizer, default_model=_profile())
    llm = TypeErrorLLM()

    with pytest.raises(TypeError, match="inside the real model invocation"):
        await manager.invoke(
            llm,
            "conversation",
            [HumanMessage(content="hello")],
            session_id="s-1",
            user_id="u-1",
        )

    assert llm.calls == 1


@pytest.mark.asyncio
async def test_tool_event_keys_are_unique_across_resume_attempts() -> None:
    store = FakeEventStore()
    manager = ContextManager(event_store=store, tokenizer=_byte_tokenizer, default_model=_profile())
    args = {"order_id": "ORD-1", "amount": "19.90", "idempotency_key": "business-key"}

    async with manager.bind_request("s-1", "u-1", "same-request", {}):
        first = await manager.record_tool_call("refund", args)
    async with manager.bind_request("s-1", "u-1", "same-request", {}):
        second = await manager.record_tool_call("refund", args)

    assert first["event_key"] != second["event_key"]
    assert first["payload"]["arguments"]["idempotency_key"] == "business-key"
    assert second["payload"]["arguments"]["idempotency_key"] == "business-key"
    assert len(store.events) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("turns", [50, 100])
async def test_long_sessions_compact_without_exceeding_final_prompt_budget(turns: int) -> None:
    store = FakeEventStore()
    cache = _memory_cache()
    profile = _profile()
    manager = ContextManager(event_store=store, cache=cache, tokenizer=_byte_tokenizer, default_model=profile)

    for turn in range(turns):
        state = {
            "session_id": "s-1",
            "user_id": "u-1",
            "messages": [HumanMessage(content=f"turn-{turn}: explain the support steps")],
            "sub_results": {"rag": {"raw_documents": ["do not put this in P1"]}},
        }
        async with manager.bind_request(
            "s-1", "u-1", f"request-{turn}", state,
            last_event_seq=len(store.events), checkpoint_version=1,
        ):
            package = await manager.build(
                "s-1", "u-1", "knowledge_rag", f"turn-{turn}: explain the support steps",
                state=state,
                evidence=[{"source": "guide", "content": "reference " * 100}],
            )
            assert package.total_tokens <= profile.prompt_budget
            assert package.diagnostics["final_prompt_within_budget"] is True
            await manager.record_tool_result({
                "success": True,
                "order_id": f"ORD-{turn:04d}",
                "amount": "15.00",
                "content": "durable full result " * 100,
            })

    assert store.digest_count >= 1
    assert store.recent_count < turns
    assert store.working_set["summary_event_seq"] >= 32
    assert len(store.working_set["archive_summary"]["provenance"]) >= (turns // 32) * 32
    snapshot = manager.metrics_snapshot()
    assert all(isinstance(value, (int, float)) for value in snapshot.values())
    assert "u-1" not in json.dumps(snapshot)
    assert "durable full result" not in json.dumps(snapshot)
    assert snapshot["context_cold_restores_total"] == 1


@pytest.mark.asyncio
async def test_synchronize_checkpoint_is_cache_only_and_owner_scoped() -> None:
    store = FakeEventStore()
    cache = _memory_cache()
    manager = ContextManager(event_store=store, cache=cache, tokenizer=_byte_tokenizer, default_model=_profile())
    checkpoint = {
        "session_id": "s-1",
        "user_id": "u-1",
        "last_event_seq": 9,
        "version": 4,
        "messages": [{"role": "user", "content": "checkpoint message"}],
        "context": {"session_state": {"pending_action": {"type": "refund_create", "order_id": "ORD-9", "amount": "22.00"}}},
    }

    updated = await manager.synchronize_checkpoint(checkpoint)
    cached = await cache.get("s-1", "u-1")

    assert updated is True
    assert store.load_count == 0
    assert cached["last_event_seq"] == 9
    assert cached["checkpoint_version"] == 4
    assert cached["recent_messages"] == [{"role": "user", "content": "checkpoint message"}]
    assert cached["requires_cold_restore"] is True
    await manager.invalidate_session("s-1", "u-1")
    assert await cache.get("s-1", "u-1") is None


@pytest.mark.asyncio
async def test_working_set_cache_sliding_ttl_and_custom_ttl(monkeypatch) -> None:
    import context.manager as manager_module

    now = [1000.0]
    monkeypatch.setattr(manager_module.time, "time", lambda: now[0])
    cache = _memory_cache(ttl=10)
    working_set = {"session_id": "s-1", "user_id": "u-1", "last_event_seq": 0}

    await cache.put("s-1", "u-1", working_set)
    now[0] += 9
    assert await cache.get("s-1", "u-1") is not None
    now[0] += 9
    assert await cache.get("s-1", "u-1") is not None
    now[0] += 11
    assert await cache.get("s-1", "u-1") is None


@pytest.mark.asyncio
async def test_owner_cache_keys_hash_unambiguous_identity_tuples() -> None:
    cache = _memory_cache()
    first_key = cache._key("c", "a:b")
    second_key = cache._key("b:c", "a")
    assert first_key != second_key

    await cache.put("c", "a:b", {"session_id": "c", "user_id": "a:b", "last_event_seq": 1})
    await cache.put("b:c", "a", {"session_id": "b:c", "user_id": "a", "last_event_seq": 2})
    assert (await cache.get("c", "a:b"))["last_event_seq"] == 1
    assert (await cache.get("b:c", "a"))["last_event_seq"] == 2


@pytest.mark.asyncio
async def test_cache_payload_owner_is_validated_after_hashed_key_lookup() -> None:
    from context.models import ContextOwnershipError

    cache = _memory_cache()
    key = cache._key("s-1", "u-1")
    await cache.short_term_memory.set_value(key, json.dumps({
        "session_id": "s-other",
        "user_id": "u-1",
        "expires_at": 9999999999,
        "working_set": {"session_id": "s-other", "user_id": "u-1"},
    }))

    with pytest.raises(ContextOwnershipError, match="owner mismatch"):
        await cache.get("s-1", "u-1")


@pytest.mark.asyncio
async def test_repeated_identical_user_messages_are_not_globally_deduplicated() -> None:
    manager = ContextManager(tokenizer=_byte_tokenizer, default_model=_profile())
    state = {
        "messages": [
            HumanMessage(content="确认"),
            AIMessage(content="第一次确认已收到"),
            HumanMessage(content="确认"),
        ]
    }
    package = await manager.build(
        "s-1", "u-1", "conversation", "确认", state=state, task_message="确认"
    )
    prompt = "\n".join(str(message.content) for message in package.messages)

    assert prompt.count('"content": "确认"') == 1
    assert "第一次确认已收到" in prompt
    assert prompt.count("<CurrentUser>") == 1


def test_required_context_overflow_fails_closed() -> None:
    manager = ContextManager(tokenizer=_byte_tokenizer, default_model=ModelProfile(
        context_limit=512,
        max_output_tokens=64,
        reserve=32,
        safety_margin=32,
        hard_ratio=0.95,
        soft_ratio=0.7,
    ))

    with pytest.raises(ContextOverflowError):
        asyncio.run(manager.build(
            "s-1", "u-1", "conversation", "x" * 10000,
            system_prompt="system",
        ))
