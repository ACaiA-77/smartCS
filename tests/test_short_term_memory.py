"""Short-term memory resilience tests for unavailable Redis."""

from __future__ import annotations

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
