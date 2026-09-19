"""Durable conversation state and message storage for one customer session."""

from __future__ import annotations

import json
from copy import deepcopy
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from memory.short_term import ShortTermMemory


@dataclass
class ConversationState:
    """Structured state kept separately from the human/assistant message log."""

    last_intent: str | None = None
    accumulated_entities: dict[str, Any] = field(default_factory=dict)
    turn_count: int = 0
    pending_action: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "last_intent": self.last_intent,
            "accumulated_entities": deepcopy(self.accumulated_entities),
            "turn_count": self.turn_count,
            "pending_action": deepcopy(self.pending_action),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ConversationState":
        if not isinstance(value, dict):
            raise ValueError("conversation state must be an object")

        last_intent = value.get("last_intent")
        if last_intent is not None and not isinstance(last_intent, str):
            raise ValueError("invalid last_intent")

        entities = value.get("accumulated_entities", {})
        if not isinstance(entities, dict):
            raise ValueError("invalid accumulated_entities")

        turn_count = value.get("turn_count", 0)
        if not isinstance(turn_count, int) or isinstance(turn_count, bool) or turn_count < 0:
            raise ValueError("invalid turn_count")

        pending = value.get("pending_action")
        if pending is not None:
            pending = _normalize_pending_action(pending)

        return cls(
            last_intent=last_intent,
            accumulated_entities=deepcopy(entities),
            turn_count=turn_count,
            pending_action=pending,
        )


def _normalize_pending_action(action: dict[str, Any]) -> dict[str, Any]:
    """Validate the only structured pending action supported by the domain."""
    if not isinstance(action, dict) or action.get("type") != "refund_create":
        raise ValueError("unsupported pending action")

    required_strings = (
        "order_id",
        "user_id",
        "refund_mode",
        "reason",
        "idempotency_key",
    )
    if any(
        not isinstance(action.get(key), str) or not action[key].strip()
        for key in required_strings
    ):
        raise ValueError("invalid pending refund action")
    amount = action.get("amount")
    if not isinstance(amount, (int, float)) or isinstance(amount, bool):
        raise ValueError("invalid pending refund amount")

    arguments = action.get("arguments")
    if not isinstance(arguments, dict):
        raise ValueError("invalid pending refund arguments")
    allowed = {"order_id", "user_id", "reason"}
    if set(arguments) != allowed or any(
        not isinstance(arguments.get(key), str) or not arguments[key].strip()
        for key in allowed
    ):
        raise ValueError("invalid pending refund arguments")

    canonical_order_id = action["order_id"].strip()
    canonical_user_id = action["user_id"].strip()
    canonical_reason = action["reason"].strip()
    if (
        arguments["order_id"].strip() != canonical_order_id
        or arguments["user_id"].strip() != canonical_user_id
        or arguments["reason"].strip() != canonical_reason
    ):
        raise ValueError("pending refund arguments do not match action")

    return {
        "type": "refund_create",
        "order_id": canonical_order_id,
        "user_id": canonical_user_id,
        "amount": amount,
        "refund_mode": action["refund_mode"].strip(),
        "reason": canonical_reason,
        "idempotency_key": action["idempotency_key"].strip(),
        "arguments": {key: arguments[key].strip() for key in allowed},
    }


class SessionStore:
    """Own conversation messages and durable structured state for one session."""

    STATE_KEY_PREFIX = "smartcs:session_state:"
    LEGACY_SNAPSHOT_PREFIX = "[wm_snapshot]"

    def __init__(self, short_term_memory: ShortTermMemory):
        self.short_term_memory = short_term_memory
        self._checkpoint_state = ContextVar(f"session_state_{id(self)}", default=None)

    @contextmanager
    def checkpoint_context(self, session_id: str, state: dict[str, Any]):
        """Use authoritative request-local state; Redis cannot resurrect stale pending actions."""
        token = self._checkpoint_state.set([session_id, ConversationState.from_dict(state)])
        try:
            yield
        finally:
            self._checkpoint_state.reset(token)

    def _state_key(self, session_id: str) -> str:
        return f"{self.STATE_KEY_PREFIX}{session_id}"

    async def get_state(self, session_id: str) -> ConversationState:
        bound = self._checkpoint_state.get()
        if bound is not None and bound[0] == session_id:
            return ConversationState.from_dict(bound[1].to_dict())
        raw = await self.short_term_memory.get_value(self._state_key(session_id))
        if raw:
            try:
                return ConversationState.from_dict(json.loads(raw))
            except (TypeError, ValueError, json.JSONDecodeError):
                pass

        # Read the old message-embedded snapshot only when the new state key is absent.
        history = await self.short_term_memory.get_history(session_id)
        for message in reversed(history):
            if message.get("role") != "system":
                continue
            content = message.get("content", "")
            if not isinstance(content, str) or not content.startswith(self.LEGACY_SNAPSHOT_PREFIX):
                continue
            try:
                snapshot = json.loads(content[len(self.LEGACY_SNAPSHOT_PREFIX) :])
                state = ConversationState.from_dict(snapshot)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            await self.save_state(session_id, state)
            return state
        return ConversationState()

    async def save_state(
        self, session_id: str, state: ConversationState | dict[str, Any]
    ) -> ConversationState:
        normalized = (
            ConversationState.from_dict(state.to_dict())
            if isinstance(state, ConversationState)
            else ConversationState.from_dict(state)
        )
        bound = self._checkpoint_state.get()
        if bound is not None and bound[0] == session_id:
            bound[1] = normalized
            return normalized
        payload = json.dumps(normalized.to_dict(), ensure_ascii=False, separators=(",", ":"))
        await self.short_term_memory.set_value(self._state_key(session_id), payload)
        return normalized

    async def update_state(
        self,
        session_id: str,
        updates: dict[str, Any] | None = None,
        **changes: Any,
    ) -> ConversationState:
        merged = dict(updates or {})
        merged.update(changes)
        current = (await self.get_state(session_id)).to_dict()
        current.update(merged)
        return await self.save_state(session_id, current)

    async def set_pending_action(
        self, session_id: str, action: dict[str, Any]
    ) -> ConversationState:
        return await self.update_state(session_id, pending_action=action)

    async def clear_pending_action(self, session_id: str) -> ConversationState:
        return await self.update_state(session_id, pending_action=None)

    async def add_message(self, session_id: str, role: str, content: str) -> None:
        await self.short_term_memory.add_message(session_id, role, content)

    async def get_history(
        self,
        session_id: str,
        last_n: int | None = None,
        *,
        include_legacy_system: bool = False,
    ) -> list[dict[str, Any]]:
        history = await self.short_term_memory.get_history(session_id)
        if not include_legacy_system:
            history = [
                message
                for message in history
                if not (
                    message.get("role") == "system"
                    and isinstance(message.get("content"), str)
                    and message["content"].startswith(self.LEGACY_SNAPSHOT_PREFIX)
                )
            ]
        if last_n:
            return history[-last_n:]
        return history

    async def get_context_window(self, session_id: str, max_tokens: int = 4000) -> str:
        history = await self.get_history(session_id)
        context_parts: list[str] = []
        estimated_tokens = 0
        for message in reversed(history):
            text = f"{message['role']}: {message['content']}"
            tokens = len(text) // 2
            if estimated_tokens + tokens > max_tokens:
                break
            context_parts.insert(0, text)
            estimated_tokens += tokens
        return "\n".join(context_parts)

    async def clear(self, session_id: str) -> None:
        await self.short_term_memory.clear(session_id)
        await self.short_term_memory.delete_value(self._state_key(session_id))


__all__ = ["ConversationState", "SessionStore"]
