"""短期记忆 — Redis 会话缓存，并在 Redis 不可用时使用有 TTL 的进程内回退。"""

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
    """短期会话记忆。

    ``ttl_seconds`` remains optional for backwards compatibility. Redis and the
    process-local fallback both expire and refresh entries on access; fallback
    state is explicitly exposed as non-Redis in ``backend_status``.
    """

    def __init__(
        self,
        redis_url: str = "redis://localhost:6379/0",
        max_turns: int = 20,
        ttl_seconds: int = 1800,
        redis_connect_timeout: float = 0.5,
        redis_retry_cooldown: float = 30.0,
    ):
        if type(ttl_seconds) is not int or ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be a positive integer")
        self.max_turns = max_turns
        self.ttl_seconds = ttl_seconds
        self._redis_url = redis_url
        self.redis_connect_timeout = redis_connect_timeout
        self.redis_retry_cooldown = redis_retry_cooldown
        self._redis: Any = None
        self._redis_disabled_until = 0.0
        self._fallback_store: dict[str, list] = {}
        self._fallback_store_expiry: dict[str, float] = {}
        self._fallback_json_store: dict[str, str] = {}
        self._fallback_json_expiry: dict[str, float] = {}
        self._json_ttl_overrides: dict[str, int] = {}
        self._last_backend = "process_fallback"

    @property
    def backend_status(self) -> dict[str, Any]:
        real_redis = self._redis is not None
        return {
            "backend": "redis" if real_redis else "process_fallback",
            "redis_available": real_redis,
            "fallback_available": True,
        }

    async def _get_redis(self):
        """懒加载Redis连接；断连期间按冷却时间回退，不循环建连。"""
        if self._redis_disabled_until > time.monotonic():
            self._last_backend = "process_fallback"
            return None
        if self._redis is None:
            if aioredis is None:
                self._last_backend = "process_fallback"
                return None
            try:
                self._redis = aioredis.from_url(
                    self._redis_url,
                    decode_responses=True,
                    socket_connect_timeout=self.redis_connect_timeout,
                    socket_timeout=self.redis_connect_timeout,
                    retry_on_timeout=False,
                )
                await self._redis.ping()
            except Exception:
                if self._redis is not None:
                    try:
                        await self._redis.aclose()
                    except Exception:
                        pass
                self._redis = None
                self._redis_disabled_until = time.monotonic() + self.redis_retry_cooldown
                self._last_backend = "process_fallback"
        if self._redis is not None:
            self._last_backend = "redis"
        return self._redis

    async def _disable_redis(self, client: Any) -> None:
        if self._redis is client:
            try:
                await client.aclose()
            except Exception:
                pass
            self._redis = None
            self._redis_disabled_until = time.monotonic() + self.redis_retry_cooldown
            self._last_backend = "process_fallback"

    def _session_key(self, session_id: str) -> str:
        return f"smartcs:short_term:{session_id}"

    def _expire_fallback_message_entry(self, session_id: str) -> bool:
        expires_at = self._fallback_store_expiry.get(session_id)
        if expires_at is not None and expires_at <= time.monotonic():
            self._fallback_store.pop(session_id, None)
            self._fallback_store_expiry.pop(session_id, None)
            return False
        return session_id in self._fallback_store

    def _expire_fallback_json_entry(self, key: str) -> str | None:
        expires_at = self._fallback_json_expiry.get(key)
        if expires_at is not None and expires_at <= time.monotonic():
            self._fallback_json_store.pop(key, None)
            self._fallback_json_expiry.pop(key, None)
            self._json_ttl_overrides.pop(key, None)
            return None
        return self._fallback_json_store.get(key)

    async def get_value(self, key: str) -> str | None:
        """Read a JSON value and slide its TTL on both backends."""
        redis = await self._get_redis()
        if redis is not None:
            try:
                value = await redis.get(key)
                if value is not None:
                    await redis.expire(key, self._json_ttl_overrides.get(key, self.ttl_seconds))
                else:
                    self._json_ttl_overrides.pop(key, None)
                return value
            except Exception:
                await self._disable_redis(redis)
        value = self._expire_fallback_json_entry(key)
        if value is not None:
            ttl = self._json_ttl_overrides.get(key, self.ttl_seconds)
            self._fallback_json_expiry[key] = time.monotonic() + ttl
        return value

    async def set_value(self, key: str, value: str, ttl_seconds: int | None = None) -> None:
        """Store JSON with an optional per-key TTL (used by WorkingSetCache)."""
        ttl = self.ttl_seconds if ttl_seconds is None else ttl_seconds
        if type(ttl) is not int or ttl <= 0:
            raise ValueError("ttl_seconds must be a positive integer")
        self._json_ttl_overrides[key] = ttl
        redis = await self._get_redis()
        if redis is not None:
            try:
                await redis.set(key, value, ex=ttl)
                return
            except Exception:
                await self._disable_redis(redis)
        self._fallback_json_store[key] = value
        self._fallback_json_expiry[key] = time.monotonic() + ttl

    async def delete_value(self, key: str) -> None:
        """Delete a value stored through :meth:`set_value`."""
        redis = await self._get_redis()
        if redis is not None:
            try:
                await redis.delete(key)
                self._json_ttl_overrides.pop(key, None)
                return
            except Exception:
                await self._disable_redis(redis)
        self._fallback_json_store.pop(key, None)
        self._fallback_json_expiry.pop(key, None)
        self._json_ttl_overrides.pop(key, None)

    async def add_message(self, session_id: str, role: str, content: str) -> None:
        """添加一条对话消息。"""
        message = {
            "role": role,
            "content": content,
            "timestamp": datetime.now().isoformat(),
        }
        redis = await self._get_redis()
        if redis is not None:
            key = self._session_key(session_id)
            try:
                await redis.rpush(key, json.dumps(message, ensure_ascii=False))
                await redis.ltrim(key, -self.max_turns, -1)
                await redis.expire(key, self.ttl_seconds)
                return
            except Exception:
                await self._disable_redis(redis)
        if not self._expire_fallback_message_entry(session_id):
            self._fallback_store[session_id] = []
        self._fallback_store[session_id].append(message)
        if len(self._fallback_store[session_id]) > self.max_turns:
            self._fallback_store[session_id] = self._fallback_store[session_id][-self.max_turns:]
        self._fallback_store_expiry[session_id] = time.monotonic() + self.ttl_seconds

    async def get_history(self, session_id: str, last_n: int | None = None) -> list[dict]:
        """获取对话历史并刷新滑动 TTL。"""
        redis = await self._get_redis()
        if redis is not None:
            key = self._session_key(session_id)
            n = self.max_turns if last_n is None else max(0, int(last_n))
            if n == 0:
                return []
            try:
                raw = await redis.lrange(key, -n, -1)
                if raw:
                    await redis.expire(key, self.ttl_seconds)
                return [json.loads(item) for item in raw]
            except Exception:
                await self._disable_redis(redis)
        if not self._expire_fallback_message_entry(session_id):
            return []
        history = self._fallback_store[session_id]
        self._fallback_store_expiry[session_id] = time.monotonic() + self.ttl_seconds
        if last_n is not None:
            n = max(0, int(last_n))
            return list(history[-n:]) if n else []
        return list(history)

    async def clear(self, session_id: str) -> None:
        """清除指定 session 的短期对话历史。"""
        redis = await self._get_redis()
        if redis is not None:
            try:
                await redis.delete(self._session_key(session_id))
                return
            except Exception:
                await self._disable_redis(redis)
        self._fallback_store.pop(session_id, None)
        self._fallback_store_expiry.pop(session_id, None)

    async def get_context_window(
        self,
        session_id: str,
        max_tokens: int | None = None,
        *,
        model: Any | None = None,
    ) -> str:
        """兼容旧入口，委托统一 tokenizer-backed history formatter。"""
        from context.budget import history_context_text

        history = await self.get_history(session_id)
        return history_context_text(history, max_tokens=max_tokens, model=model)
