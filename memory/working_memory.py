"""Working-memory storage with versioned, TTL-bound intent entities."""

from __future__ import annotations

import threading
from collections import defaultdict
from datetime import datetime
from typing import Any


class WorkingMemory:
    """Thread-safe per-session context and bounded entity state."""

    def __init__(self, max_entries_per_session: int = 50):
        self._store: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self._context: dict[str, dict[str, Any]] = defaultdict(dict)
        self._entity_state: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
        self._lock = threading.Lock()
        self._max_entries = max_entries_per_session

    def update(self, session_id: str, data: dict[str, Any]) -> None:
        with self._lock:
            entry = {"timestamp": datetime.now().isoformat(), "data": data}
            self._store[session_id].append(entry)
            if len(self._store[session_id]) > self._max_entries:
                self._store[session_id] = self._store[session_id][-self._max_entries :]
            self._context[session_id].update(data)

    def merge_entities(self, session_id: str, entities: dict[str, Any], confirmed_turn: int) -> None:
        """Record only non-empty entities, replacing values explicitly corrected in later turns."""
        if confirmed_turn < 0:
            raise ValueError("confirmed_turn must not be negative")
        with self._lock:
            for key, value in entities.items():
                if value is None or value == "":
                    continue
                self._entity_state[session_id][key] = {"value": value, "confirmed_turn": confirmed_turn}
            self._context[session_id]["entity_state"] = {
                key: dict(value) for key, value in self._entity_state[session_id].items()
            }

    def get_active_entities(self, session_id: str, ttl_turns: int, current_turn: int) -> dict[str, Any]:
        if ttl_turns < 1:
            raise ValueError("ttl_turns must be positive")
        with self._lock:
            if not self._entity_state.get(session_id):
                for key, value in (self._context.get(session_id, {}).get("accumulated_entities", {}) or {}).items():
                    if value is not None and value != "":
                        self._entity_state[session_id][key] = {"value": value, "confirmed_turn": current_turn}
            active = {
                key: item["value"]
                for key, item in self._entity_state.get(session_id, {}).items()
                if current_turn - int(item["confirmed_turn"]) <= ttl_turns
            }
            self._context[session_id]["accumulated_entities"] = dict(active)
            return active

    def get_context(self, session_id: str) -> dict[str, Any]:
        with self._lock:
            return dict(self._context.get(session_id, {}))

    def get_history(self, session_id: str, last_n: int = 10) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._store.get(session_id, [])[-last_n:])

    def clear(self, session_id: str) -> None:
        with self._lock:
            self._store.pop(session_id, None)
            self._context.pop(session_id, None)
            self._entity_state.pop(session_id, None)

    def export_for_persistence(self, session_id: str) -> dict[str, Any]:
        return {
            "session_id": session_id,
            "context": self.get_context(session_id),
            "history": self.get_history(session_id),
            "exported_at": datetime.now().isoformat(),
        }
