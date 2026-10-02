"""Short-term memory resilience tests for unavailable Redis."""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest

import memory.short_term as short_term_module
from memory.short_term import ShortTermMemory


class FailingRedisClient:
    async def ping(self) -> None:
        raise ConnectionError("Redis is unavailable")

    async def aclose(self) -> None:
        return None


class FailingRedisFactory:
    calls = 0

    @classmethod
    def from_url(cls, *_: object, **__: object) -> FailingRedisClient:
        cls.calls += 1
        return FailingRedisClient()


class CommandFailingRedisClient:
    async def ping(self) -> None:
        return None

    async def get(self, _key: str) -> str | None:
        raise ConnectionError("Redis failed after initial ping")

    async def aclose(self) -> None:
        return None


class CommandFailingRedisFactory:
    @staticmethod
    def from_url(*_: object, **__: object) -> CommandFailingRedisClient:
        return CommandFailingRedisClient()


@pytest.mark.asyncio
async def test_unavailable_redis_uses_fallback_without_repeated_connection_attempts(monkeypatch) -> None:
    FailingRedisFactory.calls = 0
    monkeypatch.setattr(short_term_module, "aioredis", FailingRedisFactory)
    memory = ShortTermMemory(redis_retry_cooldown=60)

    await memory.add_message("session-1", "user", "第一条消息")
    await memory.add_message("session-1", "assistant", "第二条消息")
    history = await memory.get_history("session-1")

    assert FailingRedisFactory.calls == 1
    assert [item["content"] for item in history] == ["第一条消息", "第二条消息"]


@pytest.mark.asyncio
async def test_command_level_redis_failure_switches_to_expiring_fallback(monkeypatch) -> None:
    monkeypatch.setattr(short_term_module, "aioredis", CommandFailingRedisFactory)
    memory = ShortTermMemory(ttl_seconds=17, redis_retry_cooldown=60)

    await memory.add_message("session-2", "user", "fallback message")
    history = await memory.get_history("session-2")

    assert [item["content"] for item in history] == ["fallback message"]
    assert memory.backend_status == {
        "backend": "process_fallback",
        "redis_available": False,
        "fallback_available": True,
    }


@pytest.mark.asyncio
async def test_context_window_defaults_to_model_profile_budget_and_keeps_explicit_limit(monkeypatch) -> None:
    import context.budget as budget_module

    monkeypatch.setattr(short_term_module, "aioredis", None)
    monkeypatch.setenv("SMARTCS_CONTEXT_LIMIT", "1000")
    monkeypatch.setenv("SMARTCS_CONTEXT_MAX_OUTPUT_TOKENS", "100")
    monkeypatch.setenv("SMARTCS_CONTEXT_RESERVE", "50")
    monkeypatch.setenv("SMARTCS_CONTEXT_SAFETY_MARGIN", "50")
    monkeypatch.setenv("SMARTCS_CONTEXT_HARD_RATIO", "0.85")

    class ExactTokenCounter:
        def __init__(self, _profile, _tokenizer=None) -> None:
            pass

        @staticmethod
        def count_text(text: str) -> int:
            return len(text)

    monkeypatch.setattr(budget_module, "TokenCounter", ExactTokenCounter)
    memory = ShortTermMemory()
    await memory.add_message("budget-session", "user", "u" * 500)
    await memory.add_message("budget-session", "assistant", "a" * 500)

    default_window = await memory.get_context_window("budget-session")
    legacy_window = await memory.get_context_window("budget-session", max_tokens=4000)

    assert default_window == "assistant: " + "a" * 500
    assert "user: " + "u" * 500 in legacy_window
    assert "assistant: " + "a" * 500 in legacy_window


@pytest.mark.asyncio
async def test_per_key_ttl_is_honored_in_fallback(monkeypatch) -> None:
    now = [100.0]
    monkeypatch.setattr(short_term_module.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(short_term_module, "aioredis", None)
    memory = ShortTermMemory(ttl_seconds=1800)
    await memory.set_value("custom-key", "value", ttl_seconds=5)

    now[0] += 4
    assert await memory.get_value("custom-key") == "value"
    now[0] += 6
    assert await memory.get_value("custom-key") is None


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.getenv("SMARTCS_CONTEXT_REDIS_TEST") != "1",
    reason="real Redis TTL is opt-in; default environment does not certify Redis behavior",
)
async def test_real_redis_working_set_ttl_is_owner_scoped_and_does_not_flush() -> None:
    from context.manager import WorkingSetCache

    short = ShortTermMemory(
        redis_url=os.getenv("REDIS_URL", "redis://localhost:6379/0"),
        ttl_seconds=2,
        redis_retry_cooldown=1,
    )
    cache = WorkingSetCache(short, ttl=2)
    session_id = "redis-context-" + uuid.uuid4().hex
    user_id = "redis-owner-" + uuid.uuid4().hex
    try:
        await cache.put(session_id, user_id, {
            "session_id": session_id,
            "user_id": user_id,
            "last_event_seq": 0,
        })
        assert short.backend_status["redis_available"] is True, "opt-in requires a reachable real Redis"
        assert await cache.get(session_id, user_id) is not None
        await asyncio.sleep(2.1)
        assert await cache.get(session_id, user_id) is None
    finally:
        await cache.invalidate(session_id, user_id)
        client = getattr(short, "_redis", None)
        if client is not None:
            await client.aclose()
