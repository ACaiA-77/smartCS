"""Bounded layered context compression over durable, append-only events.

These helpers only change prompt previews. Durable conversation events are never
mutated or removed, and every derived summary carries event provenance.
"""

from __future__ import annotations

from collections import OrderedDict
from copy import deepcopy
import json
from typing import Any, Callable

from context.models import ContextBlock, ModelProfile


PROTECTED_KEYS = {
    "order_id",
    "ticket_id",
    "refund_id",
    "user_id",
    "client_request_id",
    "idempotency_key",
    "amount",
    "currency",
    "timestamp",
    "pending_action",
    "orderId",
    "ticketId",
    "refundId",
    "clientRequestId",
    "idempotencyKey",
}
NOISE_KEYS = {
    "debug",
    "headers",
    "trace",
    "trace_id",
    "span_id",
    "stack",
    "raw_headers",
    "authorization",
    "cookie",
    "request_id",
}
ARCHIVE_KEYS = (
    "goals",
    "resolved_topics",
    "confirmed_facts",
    "important_decisions",
    "unresolved",
    "referenced_entities",
    "provenance",
)


_EVENT_ALIASES = {
    "USER_MESSAGE": "user",
    "ASSISTANT_MESSAGE": "assistant",
    "MESSAGE": "message",
    "TOOL_CALL": "tool_call",
    "TOOL_RESULT": "tool_result",
}


def normalize_event_type(event_type: Any, payload: Any | None = None) -> str:
    raw = str(event_type or "event")
    normalized = _EVENT_ALIASES.get(raw.upper(), raw.lower())
    if normalized == "message" and isinstance(payload, dict):
        role = str(payload.get("role", "message")).lower()
        return role if role in {"user", "assistant"} else "message"
    return normalized


def _json(value: Any, *, max_chars: int | None = None) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    if max_chars is not None and len(text) > max_chars:
        return text[: max(0, max_chars - 24)] + "…<truncated_preview>"
    return text


def _set_path(target: dict[str, Any], path: list[str], value: Any) -> None:
    """Set a nested path without assuming intermediate values are mappings."""
    cursor = target
    for item in path[:-1]:
        nested = cursor.get(item)
        if not isinstance(nested, dict):
            nested = {}
            cursor[item] = nested
        cursor = nested
    cursor[path[-1]] = deepcopy(value)


def extract_protected_fields(value: Any, *, prefix: list[str] | None = None) -> dict[str, Any]:
    """Extract protected identifiers/actions without copying arbitrary tool args."""
    prefix = prefix or []
    found: dict[str, Any] = {}
    if isinstance(value, dict):
        for key, item in value.items():
            path = [*prefix, str(key)]
            if str(key) in PROTECTED_KEYS:
                _set_path(found, path, item)
            nested = extract_protected_fields(item, prefix=path)
            _merge_nested(found, nested)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            nested = extract_protected_fields(item, prefix=[*prefix, str(index)])
            _merge_nested(found, nested)
    return found


def _merge_nested(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    for key, value in right.items():
        if isinstance(value, dict):
            if not isinstance(left.get(key), dict):
                left[key] = {}
            _merge_nested(left[key], value)
        else:
            left[key] = deepcopy(value)
    return left


def drop_noise(value: Any) -> Any:
    """L2: remove debug/header/trace noise while retaining protected fields."""
    if isinstance(value, dict):
        return {
            key: drop_noise(item)
            for key, item in value.items()
            if str(key).lower() not in NOISE_KEYS
        }
    if isinstance(value, list):
        return [drop_noise(item) for item in value]
    return value


def _tool_success(payload: dict[str, Any]) -> bool | None:
    for key in ("success", "ok"):
        if isinstance(payload.get(key), bool):
            return payload[key]
    status = str(payload.get("status", "")).lower()
    if status in {"success", "succeeded", "completed", "ok"}:
        return True
    if status in {"failure", "failed", "error", "rejected"} or payload.get("error"):
        return False
    return None


def tool_result_preview(event: dict[str, Any], *, max_chars: int):
    """L1: bounded structured preview; full payload remains in durable storage."""
    payload = event.get("payload", event)
    cleaned = drop_noise(payload)
    protected = extract_protected_fields(cleaned)
    event_type = normalize_event_type(event.get("event_type", "tool_result"), cleaned)
    preview = {
        "event_type": event_type,
        "seq": event.get("seq"),
        "event_id": event.get("event_id"),
        "name": (
            cleaned.get("name") or cleaned.get("tool_name")
            if isinstance(cleaned, dict)
            else None
        ),
        "success": _tool_success(cleaned) if isinstance(cleaned, dict) else None,
        "protected_fields": protected,
    }
    text = _json(cleaned, max_chars=max_chars)
    preview["preview"] = text
    preview["full_payload_durable"] = True
    preview["preview_truncated"] = len(_json(cleaned)) > len(text)
    return preview


def rolling_summary_from_events(events: list[dict[str, Any]], *, max_items: int = 12) -> str:
    """L3 deterministic rolling summary derived only from original event payloads."""
    lines: list[str] = []
    for event in events[-max_items:]:
        payload = drop_noise(event.get("payload", {}))
        event_type = normalize_event_type(event.get("event_type", "event"), payload)
        provenance = f"seq={event.get('seq', '?')}; event_id={event.get('event_id', '?')}; type={event.get('event_type', 'event')}"
        if event_type in {"user", "assistant", "message"}:
            role = payload.get("role", event_type) if isinstance(payload, dict) else event_type
            content = payload.get("content", "") if isinstance(payload, dict) else str(payload)
            lines.append(f"- [{provenance}] {role}: {str(content)[:240]}")
        else:
            protected = extract_protected_fields(payload)
            if protected:
                lines.append(f"- [{provenance}] protected={_json(protected, max_chars=360)}")
            else:
                lines.append(f"- [{provenance}] {_json(payload, max_chars=240)}")
    return "\n".join(lines)


def archive_from_events(events: list[dict[str, Any]]) -> dict[str, Any]:
    """L4 archive from original events, retaining source sequence/event identity."""
    archive = {key: [] for key in ARCHIVE_KEYS}
    seen_entities = OrderedDict()
    for event in events:
        seq = event.get("seq")
        payload = event.get("payload", {})
        protected = extract_protected_fields(payload)
        if protected:
            seen_entities[str(event.get("event_id", seq))] = protected
        if isinstance(payload, dict):
            for key, archive_key in (
                ("goal", "goals"),
                ("decision", "important_decisions"),
                ("resolved_topic", "resolved_topics"),
                ("unresolved", "unresolved"),
                ("fact", "confirmed_facts"),
            ):
                if payload.get(key) is not None:
                    archive[archive_key].append(deepcopy(payload[key]))
        archive["provenance"].append({
            "seq": seq,
            "event_id": event.get("event_id"),
            "event_type": event.get("event_type"),
        })
    archive["referenced_entities"] = list(seen_entities.values())
    return archive


def merge_archives(existing: Any, incoming: dict[str, Any]) -> dict[str, Any]:
    """Append an archive delta without losing prior facts or provenance."""
    result = deepcopy(existing) if isinstance(existing, dict) else {}
    for key in ARCHIVE_KEYS:
        old_values = result.get(key)
        if not isinstance(old_values, list):
            result[key] = []
    for key in ARCHIVE_KEYS:
        old = result[key]
        seen = {_json(existing_value) for existing_value in old}
        for value in incoming.get(key, []) if isinstance(incoming, dict) else []:
            serialized = _json(value)
            if serialized not in seen:
                old.append(deepcopy(value))
                seen.add(serialized)
    return result


def archive_prompt_preview(archive: Any, *, category_items: int = 4, provenance_items: int = 8) -> dict[str, Any]:
    """Create a bounded prompt view without modifying the complete durable archive."""
    if not isinstance(archive, dict):
        return {}
    preview: dict[str, Any] = {}
    for key in ARCHIVE_KEYS:
        values = archive.get(key)
        if not isinstance(values, list) or not values:
            continue
        limit = provenance_items if key == "provenance" else category_items
        recent = values[-limit:]
        bounded = []
        for value in recent:
            if len(_json(value)) > 320:
                bounded.append(_json(value, max_chars=320))
            else:
                bounded.append(deepcopy(value))
        preview[key] = bounded
    return preview


def summary_prompt_preview(
    rolling_summary: Any,
    archive: Any,
    *,
    token_counter: Any,
    max_tokens: int,
) -> dict[str, Any]:
    """Bound the P3 prompt summary while keeping the complete archive durable."""
    if type(max_tokens) is not int or max_tokens <= 0:
        raise ValueError("max_tokens must be a positive integer")
    rolling_lines = [line for line in str(rolling_summary or "").splitlines() if line][-12:]
    parts: dict[str, Any] = {
        "rolling_summary": "\n".join(rolling_lines),
        "archive_summary": archive_prompt_preview(archive),
    }
    category_order = (
        "referenced_entities",
        "goals",
        "resolved_topics",
        "unresolved",
        "confirmed_facts",
        "important_decisions",
        "provenance",
    )

    def encoded() -> str:
        return json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str, indent=2)

    def token_count() -> int:
        return token_counter.count_text(encoded())

    attempts = 0
    while token_count() > max_tokens and attempts < 64:
        attempts += 1
        if len(rolling_lines) > 1:
            rolling_lines.pop(0)
            parts["rolling_summary"] = "\n".join(rolling_lines)
            continue
        preview = parts["archive_summary"]
        removable = next(
            (key for key in category_order if isinstance(preview.get(key), list) and len(preview[key]) > 1),
            None,
        )
        if removable:
            preview[removable].pop(0)
            continue
        if rolling_lines and len(rolling_lines[0]) > 160:
            line = rolling_lines[0]
            rolling_lines[0] = line[:120] + " … " + line[-32:]
            parts["rolling_summary"] = "\n".join(rolling_lines)
            continue
        break
    return parts


def _block_tokens(block: ContextBlock, token_counter: Any) -> int:
    text = f"<{block.name}>\n{block.content}\n</{block.name}>" if block.content else ""
    return token_counter.count_text(text)


def _compact_history(content: str) -> str:
    try:
        value = json.loads(content)
    except (TypeError, json.JSONDecodeError):
        lines = content.splitlines()
        return "\n".join(lines[-max(1, len(lines) // 2):])
    if isinstance(value, list) and len(value) > 4:
        keep = max(4, len(value) // 2)
        return json.dumps(value[-keep:], ensure_ascii=False, separators=(",", ":"))
    if isinstance(value, dict):
        # History is a prompt-only copy; retain only the newest entries if it
        # wraps the structured messages object.
        for key in ("messages", "history", "recent_messages"):
            items = value.get(key)
            if isinstance(items, list) and len(items) > 4:
                value[key] = items[-max(4, len(items) // 2):]
                break
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return content


def _compact_evidence(content: str) -> str:
    try:
        value = json.loads(content)
    except (TypeError, json.JSONDecodeError):
        return content[: max(1, len(content) // 2)]
    changed = False
    if isinstance(value, dict):
        for key in ("retrieval", "tool_events"):
            items = value.get(key)
            if isinstance(items, list) and len(items) > 1:
                keep = max(1, len(items) // 2)
                # Retrieval hits are relevance-ranked; tool events are chronological
                # and their newest results are the current task's strongest evidence.
                value[key] = items[:keep] if key == "retrieval" else items[-keep:]
                changed = True
        if not changed:
            for key, item in value.items():
                if isinstance(item, str) and len(item) > 80:
                    value[key] = item[: max(40, len(item) // 2)] + "…<prompt-preview-compacted>"
                    changed = True
                    break
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")) if changed else content


def _compact_summary(content: str) -> str:
    try:
        value = json.loads(content)
    except (TypeError, json.JSONDecodeError):
        lines = content.splitlines()
        return "\n".join(lines[-max(1, len(lines) // 2):])
    if isinstance(value, dict):
        rolling = value.get("rolling_summary")
        changed = False
        if isinstance(rolling, str) and rolling:
            lines = rolling.splitlines()
            if len(lines) > 1:
                value["rolling_summary"] = "\n".join(lines[-max(1, len(lines) // 2):])
                changed = True
        archive = value.get("archive_summary")
        if isinstance(archive, dict):
            for key in (
                "referenced_entities", "goals", "resolved_topics", "unresolved",
                "confirmed_facts", "important_decisions", "provenance",
            ):
                items = archive.get(key)
                if isinstance(items, list) and len(items) > 1:
                    archive[key] = items[-max(1, len(items) // 2):]
                    changed = True
                    break
        return json.dumps(value, ensure_ascii=False, separators=(",", ":")) if changed else content
    return content


def compact_blocks_near_limit(
    blocks: list[ContextBlock],
    *,
    profile: ModelProfile,
    token_counter: Any,
    diagnostics: dict[str, Any],
    target_tokens: int | None = None,
    hard: bool = True,
    measure: Callable[[list[ContextBlock]], int] | None = None,
) -> list[ContextBlock]:
    """L5 prompt-only compaction, finite and separately soft/hard staged.

    Soft compaction rolls older chat/summary text first and never trims Evidence.
    Hard compaction may reduce bounded evidence previews after that. This function
    does not delete durable events, protected fields, or stored archive entries.
    """
    target = target_tokens or profile.prompt_budget
    current = [deepcopy(block) for block in blocks]
    attempts = int(diagnostics.get("compression_attempts", 0))
    max_attempts = profile.compression_attempts
    diagnostics.setdefault("compression_count", 0)
    diagnostics.setdefault("compression_layers", [])

    current_tokens = measure(current) if measure is not None else sum(block.tokens for block in current)
    while current_tokens > target and attempts < max_attempts:
        candidates: list[tuple[int, int, ContextBlock]] = []
        for index, block in enumerate(current):
            exhaustion_key = "hard_compaction_exhausted" if hard else "soft_compaction_exhausted"
            if block.metadata.get(exhaustion_key) or block.required or block.priority < 2:
                continue
            if hard and block.name == "StatusBar":
                candidates.append((0, index, block))
            elif block.name == "Summary":
                candidates.append((1, index, block))
            elif block.name == "RecentHistory":
                candidates.append((2, index, block))
            elif hard and block.name == "Evidence":
                candidates.append((3, index, block))
        if not candidates:
            break
        _, index, victim = min(candidates, key=lambda item: (item[0], -item[2].tokens))
        original = victim.content
        if victim.name == "RecentHistory":
            victim.content = _compact_history(victim.content)
        elif victim.name == "Summary":
            victim.content = _compact_summary(victim.content)
        elif victim.name == "Evidence":
            victim.content = _compact_evidence(victim.content)
        else:
            current.pop(index)
            attempts += 1
            diagnostics["compression_count"] += 1
            diagnostics["compression_layers"].append("L5-status-preview-drop")
            continue
        if victim.content == original:
            # Leave irreducible blocks intact here; the assembler's final eviction
            # stage applies the same explicit priority ordering after other blocks
            # have been compacted.
            candidates_without = [item for item in candidates if item[1] != index]
            if candidates_without:
                victim.metadata[exhaustion_key] = True
                attempts += 1
                continue
            break
        else:
            victim.metadata["compacted"] = True
            diagnostics["compression_layers"].append(
                f"L5-{victim.name.lower()}" if hard else f"L3-soft-{victim.name.lower()}"
            )
        attempts += 1
        diagnostics["compression_count"] += 1
        for block in current:
            block.tokens = _block_tokens(block, token_counter)
        current_tokens = measure(current) if measure is not None else sum(block.tokens for block in current)
    diagnostics["compression_attempts"] = attempts
    return current


__all__ = [
    "ARCHIVE_KEYS",
    "PROTECTED_KEYS",
    "archive_from_events",
    "archive_prompt_preview",
    "compact_blocks_near_limit",
    "drop_noise",
    "extract_protected_fields",
    "merge_archives",
    "normalize_event_type",
    "rolling_summary_from_events",
    "summary_prompt_preview",
    "tool_result_preview",
]
