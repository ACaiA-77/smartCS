"""Pure checkpoint and working-set projections.

These rules decide what an authoritative checkpoint or a cached working set
LOOKS like after projection: which state fields are safe to persist, which
messages survive formatting, and how a checkpoint snapshot merges with cached
history. They perform no I/O, read no cache and emit no metrics — the manager
keeps the ordering of those side effects.

Names are re-exported by ``context.manager`` under their previous private
aliases, so every call site and patch point stays where it was.
"""

from __future__ import annotations

from copy import deepcopy
import json
import math
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage

from context.compression import drop_noise, extract_protected_fields
from context.models import ContextCompressionError, ContextOwnershipError

MISSING = object()


def validate_owner(session_id: str, user_id: str) -> None:
    for name, value in {"session_id": session_id, "user_id": user_id}.items():
        if not isinstance(value, str) or not value.strip() or len(value) > 128 or "\x00" in value:
            raise ContextOwnershipError(f"invalid {name}")


def validate_working_set_owner(working_set: dict[str, Any], session_id: str, user_id: str) -> None:
    if not isinstance(working_set, dict):
        raise ContextOwnershipError("working set must be an object")
    ws_session = working_set.get("session_id")
    ws_user = working_set.get("user_id")
    if ws_session not in (None, session_id) or ws_user not in (None, user_id):
        raise ContextOwnershipError("working set owner mismatch")


def json_safe(value: Any) -> Any:
    """Copy only finite JSON values so P1 state cannot contain arbitrary objects."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, list):
        return [json_safe(item) for item in value]
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items() if isinstance(key, str)}
    return None


def safe_state(state: dict[str, Any]) -> dict[str, Any]:
    """P1 request state: scalar workflow facts and the exact pending action only."""
    allowed_scalars = {
        "intent",
        "current_agent",
        "needs_clarification",
        "compliance_passed",
        "client_request_id",
        "last_intent",
        "turn_count",
    }
    nested_session = state.get("session_state")
    if not isinstance(nested_session, dict) or not nested_session:
        sub_results = state.get("sub_results")
        nested_session = sub_results.get("_session_context") if isinstance(sub_results, dict) else None
    if not isinstance(nested_session, dict):
        nested_session = {}
    safe: dict[str, Any] = {}
    for key in allowed_scalars:
        value = state.get(key, nested_session.get(key))
        if key in state or key in nested_session:
            if isinstance(value, (str, bool, int, float)) or value is None:
                safe[key] = json_safe(value)
    if "pending_action" in state:
        pending = state["pending_action"]
        safe["pending_action"] = json_safe(pending) if isinstance(pending, dict) else None
    elif "pending_action" in nested_session:
        pending = nested_session["pending_action"]
        safe["pending_action"] = json_safe(pending) if isinstance(pending, dict) else None
    entities = state.get("accumulated_entities", nested_session.get("accumulated_entities"))
    if isinstance(entities, dict):
        critical = {
            key: json_safe(value)
            for key, value in entities.items()
            if str(key).lower() in {
                "order_id", "ticket_id", "refund_id", "product", "serial_number", "client_request_id"
            } and isinstance(value, (str, int, float, bool))
        }
        if critical:
            safe["accumulated_entities"] = critical
    return drop_noise(safe)


def safe_session_state(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    allowed = {"last_intent", "turn_count", "pending_action", "accumulated_entities"}
    result = safe_state({key: value[key] for key in allowed if key in value})
    # Keep only critical scalar entities in the structured checkpoint state.
    entities = value.get("accumulated_entities")
    if isinstance(entities, dict):
        result.update(safe_state({"accumulated_entities": entities}))
    return result


def decode_archive(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return deepcopy(value)
    if isinstance(value, str) and value:
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ContextCompressionError("stored archive summary is not valid JSON") from exc
        if not isinstance(decoded, dict):
            raise ContextCompressionError("stored archive summary must be an object")
        return decoded
    return {}


def normalise_working_set(value: dict[str, Any], session_id: str, user_id: str) -> dict[str, Any]:
    validate_working_set_owner(value, session_id, user_id)
    result = deepcopy(value)
    result["session_id"] = session_id
    result["user_id"] = user_id
    result.setdefault("recent_messages", [])
    result.setdefault("recent_tool_events", [])
    result.setdefault("rolling_summary", "")
    result["archive_summary"] = decode_archive(result.get("archive_summary"))
    result.setdefault("session_state", {})
    result.setdefault("last_event_seq", 0)
    result.setdefault("version", 0)
    result.setdefault("checkpoint_version", 0)
    result.setdefault("cutoff_seq", 0)
    result.setdefault("protected_fields", {})
    result.setdefault("summary_event_seq", 0)
    return result


def message_role(message: Any) -> str:
    if isinstance(message, HumanMessage):
        return "user"
    if isinstance(message, AIMessage):
        return "assistant"
    role = message.get("role") if isinstance(message, dict) else getattr(message, "role", None)
    if role in {"user", "assistant", "tool"}:
        return role
    kind = str(getattr(message, "type", "message"))
    return "tool" if kind == "tool" else kind


def message_content(message: Any) -> str:
    content = message.get("content", "") if isinstance(message, dict) else getattr(message, "content", message)
    if isinstance(content, str):
        return content
    return json.dumps(content, ensure_ascii=False, default=str)


def format_messages(messages: list[Any], *, limit: int) -> list[dict[str, str]]:
    formatted = []
    for message in messages:
        role = message_role(message)
        content = message_content(message)
        if role in {"user", "assistant", "tool"} and content:
            formatted.append({"role": role, "content": content})
    return formatted[-limit:]


def checkpoint_value(checkpoint: Any, name: str, default: Any = None) -> Any:
    if isinstance(checkpoint, dict):
        return checkpoint.get(name, default)
    return getattr(checkpoint, name, default)


def message_sequence_offset(haystack: list[dict[str, str]], needle: list[dict[str, str]]) -> int | None:
    if not needle or len(needle) > len(haystack):
        return None
    for start in range(len(haystack) - len(needle) + 1):
        if haystack[start : start + len(needle)] == needle:
            return start
    return None


def merge_checkpoint_messages(
    existing: list[dict[str, str]],
    incoming: list[dict[str, str]],
    *,
    request_id: str | None,
    prior_request_id: str | None,
    limit: int = 20,
) -> list[dict[str, str]]:
    """Merge checkpoint snapshots by event-order overlap, preserving new request duplicates."""
    old = list(existing)
    new = list(incoming)
    if not new:
        return old[-limit:]
    if not old:
        return new[-limit:]
    same_request = request_id is not None and request_id == prior_request_id
    if not same_request and len(new) <= 2:
        # A one-message request or its user/assistant pair is a new event even if
        # every role/content value exactly matches an earlier request.
        return [*old, *new][-limit:]
    if same_request and message_sequence_offset(old, new) is not None:
        return old[-limit:]
    old_offset_in_new = message_sequence_offset(new, old)
    if old_offset_in_new is not None:
        if old_offset_in_new + len(old) < len(new):
            # The new snapshot contains the prior suffix and later messages.
            return new[-limit:]
        if len(new) > 2:
            # A longer/older full snapshot without a new tail must not duplicate
            # the cached history; short new-request pairs were handled above.
            return old[-limit:]
    overlap = 0
    for size in range(min(len(old), len(new)), 0, -1):
        if old[-size:] == new[:size]:
            overlap = size
            break
    if overlap:
        return [*old, *new[overlap:]][-limit:]
    return [*old, *new][-limit:]


def checkpoint_context(checkpoint: Any) -> dict[str, Any]:
    context = checkpoint_value(checkpoint, "context", {})
    return context if isinstance(context, dict) else {}


def checkpoint_session_state(checkpoint: Any) -> dict[str, Any]:
    context = checkpoint_context(checkpoint)
    session_state = context.get("session_state", {})
    session_state = deepcopy(session_state) if isinstance(session_state, dict) else {}
    pending_action = checkpoint_value(checkpoint, "pending_action", MISSING)
    if pending_action is not MISSING:
        # The checkpoint's explicit null is authoritative after cancel/finish.
        session_state["pending_action"] = deepcopy(pending_action)
    return safe_session_state(session_state)


def checkpoint_cutoff_seq(checkpoint: Any) -> int | None:
    context = checkpoint_context(checkpoint)
    candidates = (
        checkpoint_value(checkpoint, "cutoff_seq", MISSING),
        context.get("cutoff_seq", MISSING),
        context.get("history_cutoff_seq", MISSING),
    )
    for value in candidates:
        if type(value) is int and value >= 0:
            return value
    return None


def checkpoint_projection(checkpoint: Any, session_id: str, user_id: str) -> dict[str, Any]:
    context = checkpoint_context(checkpoint)
    session_state = checkpoint_session_state(checkpoint)
    messages = checkpoint_value(checkpoint, "messages", []) or []
    last_event_seq = int(checkpoint_value(checkpoint, "last_event_seq", 0) or 0)
    checkpoint_version = int(checkpoint_value(checkpoint, "version", 0) or 0)
    cutoff_seq = checkpoint_cutoff_seq(checkpoint)
    projection = {
        "session_id": session_id,
        "user_id": user_id,
        "recent_messages": format_messages(list(messages), limit=20),
        "synchronized_request_id": context.get("request_id"),
        "recent_tool_events": [],
        "rolling_summary": "",
        "archive_summary": {},
        "session_state": session_state,
        "last_event_seq": last_event_seq,
        "version": 0,
        "checkpoint_version": checkpoint_version,
        "protected_fields": extract_protected_fields(session_state),
        "summary_event_seq": cutoff_seq or 0,
        "requires_cold_restore": True,
    }
    if cutoff_seq is not None:
        projection["cutoff_seq"] = cutoff_seq
    return projection
