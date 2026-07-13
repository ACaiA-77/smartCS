"""Short-term conversation memory with fast fallback when Redis is unavailable."""

from __future__ import annotations

import json
import time
from datetime import datetime
from typing import Any

try:
    import redis.asyncio as aioredis
except ImportError:
    aioredis = None


class ShortTermMemory:
    """Redis-backed memory with a bounded in-process fallback circuit breaker."""

    def __init__(
        self,
        redis_url: str = "redis://localhost:6379/0",
        max_turns: int = 20,
        ttl_seconds: int = 1800,
        redis_unavailable_retry_seconds: float = 30.0,
        redis_connect_timeout_seconds: float = 0.5,
    ):
        if redis_unavailable_retry_seconds <= 0:
            raise ValueError("redis_unavailable_retry_seconds must be positive")
        if redis_connect_timeout_seconds <= 0:
            raise ValueError("redis_connect_timeout_seconds must be positive")
        self.max_turns = max_turns
        self.ttl_seconds = ttl_seconds
        self._redis_url = redis_url
        self._redis: Any = None
        self._fallback_store: dict[str, list[dict[str, Any]]] = {}
        self._redis_unavailable_retry_seconds = redis_unavailable_retry_seconds
        self._redis_connect_timeout_seconds = redis_connect_timeout_seconds
        self._redis_retry_after = 0.0

    async def _get_redis(self):
        """Return Redis when available; otherwise use a timed memory-fallback window."""
        if self._redis is not None:
            return self._redis
        if aioredis is None or time.monotonic() < self._redis_retry_after:
            return None

        candidate = None
        try:
            candidate = aioredis.from_url(
                self._redis_url,
                decode_responses=True,
                socket_connect_timeout=self._redis_connect_timeout_seconds,
                socket_timeout=self._redis_connect_timeout_seconds,
            )
            await candidate.ping()
            self._redis = candidate
            self._redis_retry_after = 0.0
        except Exception:
            close = getattr(candidate, "aclose", None)
            if close is not None:
                await close()
            self._redis = None
            self._redis_retry_after = time.monotonic() + self._redis_unavailable_retry_seconds
        return self._redis

    def _session_key(self, session_id: str) -> str:
        return f"smartcs:short_term:{session_id}"

    async def health_status(self) -> dict[str, Any]:
        client = await self._get_redis()
        return {
            "ready": client is not None,
            "mode": "redis" if client is not None else "memory",
            "retrying_after_seconds": max(0.0, round(self._redis_retry_after - time.monotonic(), 2)),
        }

    async def close(self) -> None:
        if self._redis is not None:
            await self._redis.aclose()
            self._redis = None

    async def add_message(self, session_id: str, role: str, content: str) -> None:
        message = {"role": role, "content": content, "timestamp": datetime.now().isoformat()}
        client = await self._get_redis()
        if client is not None:
            key = self._session_key(session_id)
            await client.rpush(key, json.dumps(message, ensure_ascii=False))
            await client.ltrim(key, -self.max_turns, -1)
            await client.expire(key, self.ttl_seconds)
            return
        self._fallback_store.setdefault(session_id, []).append(message)
        if len(self._fallback_store[session_id]) > self.max_turns:
            self._fallback_store[session_id] = self._fallback_store[session_id][-self.max_turns :]

    async def get_history(self, session_id: str, last_n: int | None = None) -> list[dict[str, Any]]:
        client = await self._get_redis()
        if client is not None:
            raw = await client.lrange(self._session_key(session_id), -(last_n or self.max_turns), -1)
            return [json.loads(item) for item in raw]
        history = self._fallback_store.get(session_id, [])
        return list(history[-last_n:] if last_n else history)

    async def clear(self, session_id: str) -> None:
        client = await self._get_redis()
        if client is not None:
            await client.delete(self._session_key(session_id))
        else:
            self._fallback_store.pop(session_id, None)

    async def get_context_window(self, session_id: str, max_tokens: int = 4000) -> str:
        history = await self.get_history(session_id)
        parts: list[str] = []
        estimated_tokens = 0
        for message in reversed(history):
            text = f"{message['role']}: {message['content']}"
            token_count = len(text) // 2
            if estimated_tokens + token_count > max_tokens:
                break
            parts.insert(0, text)
            estimated_tokens += token_count
        return "\n".join(parts)
