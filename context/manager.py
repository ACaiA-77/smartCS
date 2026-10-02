"""Central request-scoped context manager for SmartCS agent/model calls.

Public integration surface:
- ``bind_request(session_id, user_id, request_id, state)`` scopes ownership.
- ``build(...)`` assembles each model prompt under a validated token budget.
- ``invoke(...)`` builds and makes exactly one model invocation.
- ``synchronize_checkpoint(cp)`` refreshes the owner cache from an already-authoritative checkpoint without a database read.
- ``record_tool_call/result`` append auditable tool events and refresh cache only after durable writes.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from contextvars import ContextVar
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import inspect
import json
import math
import time
from typing import Any
from uuid import uuid4

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from context.assembler import ContextAssembler, DYNAMIC_ORDER
from context.budget import TokenCounter, profile_from_model
from context.compression import (
    archive_from_events,
    drop_noise,
    extract_protected_fields,
    merge_archives,
    normalize_event_type,
    rolling_summary_from_events,
    summary_prompt_preview,
    tool_result_preview,
)
from context.models import (
    ContextBlock,
    ContextCompressionError,
    ContextOverflowError,
    ContextOwnershipError,
    ContextPackage,
    ModelProfile,
    RequestContext,
)
from context.policies import policy_for


active_context: ContextVar[RequestContext | None] = ContextVar("smartcs_active_context", default=None)


_CACHE_TTL_SECONDS = 1800
_COMPACTION_EVENT_GAP = 32
_RECENT_EVENT_LIMIT = 100
_SUMMARY_EVENT_LIMIT = 500
_CONTEXT_BLOCK_METRIC_NAMES = {
    "System": "system",
    "ToolSchemas": "tool_schemas",
    "UserProfileCards": "user_profile_cards",
    "RetrievedUserMemory": "retrieved_user_memory",
    "SessionState": "session_state",
    "Evidence": "evidence",
    "Summary": "summary",
    "RecentHistory": "recent_history",
    "CurrentUser": "current_user",
    "ProtectedFields": "protected_fields",
    "StatusBar": "status_bar",
}


class WorkingSetCache:
    """Owner-scoped sliding-TTL cache backed by ShortTermMemory transport."""

    PREFIX = "smartcs:context:working_set:"

    def __init__(self, short_term_memory: Any, ttl: int = _CACHE_TTL_SECONDS) -> None:
        if type(ttl) is not int or ttl <= 0:
            raise ValueError("ttl must be a positive integer")
        self.short_term_memory = short_term_memory
        self.ttl = ttl

    @property
    def backend_status(self) -> dict[str, Any]:
        status = getattr(self.short_term_memory, "backend_status", None)
        if isinstance(status, dict):
            return dict(status)
        return {"backend": "unknown", "redis_available": False}

    def _key(self, session_id: str, user_id: str) -> str:
        _validate_owner(session_id, user_id)
        owner_digest = hashlib.sha256(
            json.dumps([user_id, session_id], ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return f"{self.PREFIX}{owner_digest}"

    async def _write(self, key: str, payload: dict[str, Any]) -> None:
        setter = getattr(self.short_term_memory, "set_value", None)
        if not callable(setter):
            return
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        if _accepts_keyword(setter, "ttl_seconds"):
            await setter(key, raw, ttl_seconds=self.ttl)
        else:
            # ``expires_at`` is independently enforced in get() for compatible
            # duck types which do not yet expose a per-value TTL parameter.
            await setter(key, raw)

    async def get(self, session_id: str, user_id: str) -> dict[str, Any] | None:
        key = self._key(session_id, user_id)
        getter = getattr(self.short_term_memory, "get_value", None)
        if not callable(getter):
            return None
        raw = await getter(key)
        if not raw:
            return None
        try:
            payload = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            await self.invalidate(session_id, user_id)
            return None
        if not isinstance(payload, dict):
            await self.invalidate(session_id, user_id)
            return None
        _validate_cached_owner(payload, session_id, user_id)
        expires_at = payload.get("expires_at")
        if not isinstance(expires_at, (int, float)) or expires_at <= time.time():
            await self.invalidate(session_id, user_id)
            return None
        working_set = payload.get("working_set")
        if not isinstance(working_set, dict):
            await self.invalidate(session_id, user_id)
            return None
        _validate_working_set_owner(working_set, session_id, user_id)
        # Sliding expiry is refreshed in the payload and in Redis/fallback TTL.
        payload["cached_at"] = datetime.now(timezone.utc).isoformat()
        payload["expires_at"] = time.time() + self.ttl
        await self._write(key, payload)
        return deepcopy(working_set)

    async def put(self, session_id: str, user_id: str, working_set: dict[str, Any]) -> None:
        key = self._key(session_id, user_id)
        _validate_working_set_owner(working_set, session_id, user_id)
        payload = {
            "session_id": session_id,
            "user_id": user_id,
            "cached_at": datetime.now(timezone.utc).isoformat(),
            "expires_at": time.time() + self.ttl,
            "ttl": self.ttl,
            "working_set": deepcopy(working_set),
        }
        await self._write(key, payload)

    async def invalidate(self, session_id: str, user_id: str) -> None:
        key = self._key(session_id, user_id)
        deleter = getattr(self.short_term_memory, "delete_value", None)
        if callable(deleter):
            await deleter(key)


def _accepts_keyword(function: Any, name: str) -> bool:
    try:
        signature = inspect.signature(function)
    except (TypeError, ValueError):
        return False
    return name in signature.parameters or any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in signature.parameters.values()
    )


def _validate_owner(session_id: str, user_id: str) -> None:
    for name, value in {"session_id": session_id, "user_id": user_id}.items():
        if not isinstance(value, str) or not value.strip() or len(value) > 128 or "\x00" in value:
            raise ContextOwnershipError(f"invalid {name}")


def _validate_cached_owner(payload: dict[str, Any], session_id: str, user_id: str) -> None:
    if payload.get("session_id") != session_id or payload.get("user_id") != user_id:
        raise ContextOwnershipError("working-set cache owner mismatch")
    working_set = payload.get("working_set")
    if isinstance(working_set, dict):
        _validate_working_set_owner(working_set, session_id, user_id)


def _validate_working_set_owner(working_set: dict[str, Any], session_id: str, user_id: str) -> None:
    if not isinstance(working_set, dict):
        raise ContextOwnershipError("working set must be an object")
    ws_session = working_set.get("session_id")
    ws_user = working_set.get("user_id")
    if ws_session not in (None, session_id) or ws_user not in (None, user_id):
        raise ContextOwnershipError("working set owner mismatch")


def _message_role(message: Any) -> str:
    if isinstance(message, HumanMessage):
        return "user"
    if isinstance(message, AIMessage):
        return "assistant"
    role = message.get("role") if isinstance(message, dict) else getattr(message, "role", None)
    if role in {"user", "assistant", "tool"}:
        return role
    kind = str(getattr(message, "type", "message"))
    return "tool" if kind == "tool" else kind


def _message_content(message: Any) -> str:
    content = message.get("content", "") if isinstance(message, dict) else getattr(message, "content", message)
    if isinstance(content, str):
        return content
    return json.dumps(content, ensure_ascii=False, default=str)


def _format_messages(messages: list[Any], *, limit: int) -> list[dict[str, str]]:
    formatted = []
    for message in messages:
        role = _message_role(message)
        content = _message_content(message)
        if role in {"user", "assistant", "tool"} and content:
            formatted.append({"role": role, "content": content})
    return formatted[-limit:]


def _render_json_block(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str, indent=2)


def _json_safe(value: Any) -> Any:
    """Copy only finite JSON values so P1 state cannot contain arbitrary objects."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items() if isinstance(key, str)}
    return None


_MISSING = object()


def _safe_state(state: dict[str, Any]) -> dict[str, Any]:
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
                safe[key] = _json_safe(value)
    if "pending_action" in state:
        pending = state["pending_action"]
        safe["pending_action"] = _json_safe(pending) if isinstance(pending, dict) else None
    elif "pending_action" in nested_session:
        pending = nested_session["pending_action"]
        safe["pending_action"] = _json_safe(pending) if isinstance(pending, dict) else None
    entities = state.get("accumulated_entities", nested_session.get("accumulated_entities"))
    if isinstance(entities, dict):
        critical = {
            key: _json_safe(value)
            for key, value in entities.items()
            if str(key).lower() in {
                "order_id", "ticket_id", "refund_id", "product", "serial_number", "client_request_id"
            } and isinstance(value, (str, int, float, bool))
        }
        if critical:
            safe["accumulated_entities"] = critical
    return drop_noise(safe)


def _safe_session_state(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    allowed = {"last_intent", "turn_count", "pending_action", "accumulated_entities"}
    result = _safe_state({key: value[key] for key in allowed if key in value})
    # Keep only critical scalar entities in the structured checkpoint state.
    entities = value.get("accumulated_entities")
    if isinstance(entities, dict):
        result.update(_safe_state({"accumulated_entities": entities}))
    return result


def _decode_archive(value: Any) -> dict[str, Any]:
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


def _normalise_working_set(value: dict[str, Any], session_id: str, user_id: str) -> dict[str, Any]:
    _validate_working_set_owner(value, session_id, user_id)
    result = deepcopy(value)
    result["session_id"] = session_id
    result["user_id"] = user_id
    result.setdefault("recent_messages", [])
    result.setdefault("recent_tool_events", [])
    result.setdefault("rolling_summary", "")
    result["archive_summary"] = _decode_archive(result.get("archive_summary"))
    result.setdefault("session_state", {})
    result.setdefault("last_event_seq", 0)
    result.setdefault("version", 0)
    result.setdefault("checkpoint_version", 0)
    result.setdefault("cutoff_seq", 0)
    result.setdefault("protected_fields", {})
    result.setdefault("summary_event_seq", 0)
    return result


def _protected_from_events(events: list[dict[str, Any]]) -> dict[str, Any]:
    protected: dict[str, Any] = {}
    for event in events:
        _deep_merge(protected, extract_protected_fields(event.get("payload", {})))
    return protected


def _deep_merge(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    for key, value in (right or {}).items():
        if isinstance(value, dict):
            if not isinstance(left.get(key), dict):
                left[key] = {}
            _deep_merge(left[key], value)
        else:
            left[key] = deepcopy(value)
    return left


def _split_messages(messages: list[Any]) -> tuple[str, str]:
    system_parts: list[str] = []
    current = ""
    for message in messages:
        if isinstance(message, SystemMessage):
            system_parts.append(_message_content(message))
        elif isinstance(message, HumanMessage):
            current = _message_content(message)
    if not current and messages:
        current = _message_content(messages[-1])
    return "\n\n".join(system_parts), current


def _object_to_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return deepcopy(value)
    if hasattr(value, "model_dump"):
        dumped = value.model_dump()
        return dumped if isinstance(dumped, dict) else {"result": dumped}
    if hasattr(value, "__dict__"):
        return dict(value.__dict__)
    return {"result": value}


def _checkpoint_value(checkpoint: Any, name: str, default: Any = None) -> Any:
    if isinstance(checkpoint, dict):
        return checkpoint.get(name, default)
    return getattr(checkpoint, name, default)


def _message_sequence_offset(haystack: list[dict[str, str]], needle: list[dict[str, str]]) -> int | None:
    if not needle or len(needle) > len(haystack):
        return None
    for start in range(len(haystack) - len(needle) + 1):
        if haystack[start : start + len(needle)] == needle:
            return start
    return None


def _merge_checkpoint_messages(
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
    if same_request and _message_sequence_offset(old, new) is not None:
        return old[-limit:]
    old_offset_in_new = _message_sequence_offset(new, old)
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


def _checkpoint_context(checkpoint: Any) -> dict[str, Any]:
    context = _checkpoint_value(checkpoint, "context", {})
    return context if isinstance(context, dict) else {}


def _checkpoint_session_state(checkpoint: Any) -> dict[str, Any]:
    context = _checkpoint_context(checkpoint)
    session_state = context.get("session_state", {})
    session_state = deepcopy(session_state) if isinstance(session_state, dict) else {}
    pending_action = _checkpoint_value(checkpoint, "pending_action", _MISSING)
    if pending_action is not _MISSING:
        # The checkpoint's explicit null is authoritative after cancel/finish.
        session_state["pending_action"] = deepcopy(pending_action)
    return _safe_session_state(session_state)


def _checkpoint_cutoff_seq(checkpoint: Any) -> int | None:
    context = _checkpoint_context(checkpoint)
    candidates = (
        _checkpoint_value(checkpoint, "cutoff_seq", _MISSING),
        context.get("cutoff_seq", _MISSING),
        context.get("history_cutoff_seq", _MISSING),
    )
    for value in candidates:
        if type(value) is int and value >= 0:
            return value
    return None


def _checkpoint_projection(checkpoint: Any, session_id: str, user_id: str) -> dict[str, Any]:
    context = _checkpoint_context(checkpoint)
    session_state = _checkpoint_session_state(checkpoint)
    messages = _checkpoint_value(checkpoint, "messages", []) or []
    last_event_seq = int(_checkpoint_value(checkpoint, "last_event_seq", 0) or 0)
    checkpoint_version = int(_checkpoint_value(checkpoint, "version", 0) or 0)
    cutoff_seq = _checkpoint_cutoff_seq(checkpoint)
    projection = {
        "session_id": session_id,
        "user_id": user_id,
        "recent_messages": _format_messages(list(messages), limit=20),
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


class ContextManager:
    """Builds, budgets, compacts, and observes all agent model prompts."""

    def __init__(
        self,
        event_store: Any | None = None,
        cache: WorkingSetCache | Any | None = None,
        user_memory: Any | None = None,
        *,
        tokenizer: Any | None = None,
        native_tokenizer: bool = False,
        default_model: ModelProfile | str | None = None,
    ) -> None:
        self.event_store = event_store
        self.cache = cache
        self.user_memory = user_memory
        self.tokenizer = tokenizer
        self.native_tokenizer = native_tokenizer
        self.default_model = default_model
        self._ephemeral_events: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self._metrics = {
            "context_builds_total": 0,
            "context_invocations_total": 0,
            "context_cache_hits_total": 0,
            "context_cache_misses_total": 0,
            "context_cold_restores_total": 0,
            "context_cold_restore_latency_ms_total": 0.0,
            "context_compactions_total": 0,
            "context_overflows_total": 0,
            "context_events_appended_total": 0,
            "context_digest_saves_total": 0,
            "context_summary_range_gaps_total": 0,
            "context_final_tokens_total": 0,
            "context_final_tokens_last": 0,
            "context_real_redis_last": 0,
            "context_cache_read_errors_total": 0,
            "context_cache_write_errors_total": 0,
            "context_checkpoint_syncs_total": 0,
            "context_user_profile_reads_total": 0,
            "context_user_profile_cards_total": 0,
            "context_retrieved_memory_queries_total": 0,
            "context_retrieved_memory_hit_queries_total": 0,
            "context_retrieved_memory_hits_total": 0,
            "context_block_tokens_total_last": 0,
        }
        for metric_name in _CONTEXT_BLOCK_METRIC_NAMES.values():
            self._metrics[f"context_block_{metric_name}_tokens_last"] = 0
            self._metrics[f"context_block_{metric_name}_ratio_last"] = 0.0
        self._last_diagnostics: dict[str, Any] = {}

    @asynccontextmanager
    async def bind_request(
        self,
        session_id: str,
        user_id: str,
        request_id: str | None = None,
        state: dict[str, Any] | None = None,
        *,
        last_event_seq: int | None = None,
        checkpoint_version: int | None = None,
        version: int | None = None,
    ):
        """Bind one owner/request; positional request_id/state remain compatible."""
        _validate_owner(session_id, user_id)
        existing = active_context.get()
        if existing is not None:
            if existing.manager is not self or existing.session_id != session_id or existing.user_id != user_id:
                raise ContextOwnershipError("cannot nest a context binding for another manager or owner")
            # Agent helpers may bind again; reuse the same execution scope, counter,
            # and request id instead of resetting tool-event identity.
            if state is not None and existing.state is None:
                existing.state = state
            if last_event_seq is not None:
                existing.trusted_last_event_seq = max(existing.trusted_last_event_seq or 0, int(last_event_seq))
            if checkpoint_version is None:
                checkpoint_version = version
            if checkpoint_version is not None:
                existing.checkpoint_version = max(existing.checkpoint_version or 0, int(checkpoint_version))
            yield existing
            return

        if checkpoint_version is None:
            checkpoint_version = version
        if state is not None:
            checkpoint = state.get("checkpoint", state.get("_checkpoint")) if isinstance(state, dict) else None
            if last_event_seq is None:
                last_event_seq = state.get("_checkpoint_last_event_seq", state.get("last_event_seq"))
            if checkpoint_version is None:
                checkpoint_version = state.get("_checkpoint_version", state.get("checkpoint_version"))
            if checkpoint is not None:
                if last_event_seq is None:
                    last_event_seq = _checkpoint_value(checkpoint, "last_event_seq")
                if checkpoint_version is None:
                    checkpoint_version = _checkpoint_value(checkpoint, "version")
        ctx = RequestContext(
            session_id=session_id,
            user_id=user_id,
            request_id=request_id or str(uuid4()),
            manager=self,
            state=state,
            trusted_last_event_seq=int(last_event_seq) if last_event_seq is not None else None,
            checkpoint_version=int(checkpoint_version) if checkpoint_version is not None else None,
            execution_id=str(uuid4()),
        )
        token = active_context.set(ctx)
        try:
            yield ctx
        finally:
            active_context.reset(token)

    async def synchronize_checkpoint(self, checkpoint: Any) -> bool:
        """Refresh cache from an already-persisted checkpoint; never reads MySQL.

        A cold cache receives a checkpoint projection marked for one cold restore,
        since a checkpoint alone does not contain digest/archive or tool previews.
        Existing hot summaries/tool previews are preserved.
        """
        session_id = str(_checkpoint_value(checkpoint, "session_id", ""))
        user_id = str(_checkpoint_value(checkpoint, "user_id", ""))
        _validate_owner(session_id, user_id)
        last_event_seq = int(_checkpoint_value(checkpoint, "last_event_seq", 0) or 0)
        checkpoint_version = int(_checkpoint_value(checkpoint, "version", 0) or 0)
        cache_get = getattr(self.cache, "get", None) if self.cache is not None else None
        working_set = None
        if callable(cache_get):
            try:
                working_set = await cache_get(session_id, user_id)
            except ContextOwnershipError:
                raise
            except Exception:
                self._metrics["context_cache_read_errors_total"] += 1
                working_set = None
        incoming_cutoff = _checkpoint_cutoff_seq(checkpoint)
        if working_set is None:
            working_set = _checkpoint_projection(checkpoint, session_id, user_id)
        else:
            working_set = _normalise_working_set(working_set, session_id, user_id)
            prior_seq = int(working_set.get("last_event_seq") or 0)
            prior_version = int(working_set.get("checkpoint_version") or 0)
            prior_cutoff = working_set.get("cutoff_seq")
            cutoff_changed = (
                incoming_cutoff is not None
                and prior_cutoff is not None
                and incoming_cutoff != int(prior_cutoff)
            ) or (incoming_cutoff is not None and prior_cutoff is None and incoming_cutoff > 0)
            generation_changed = (
                cutoff_changed
                or checkpoint_version < prior_version
                or last_event_seq < prior_seq
                or (
                    checkpoint_version <= 1
                    and prior_version >= checkpoint_version
                    and last_event_seq > prior_seq
                )
            )
            if generation_changed:
                # A version reset/cutoff change denotes a new checkpoint generation.
                # Never merge any cache payload from the prior generation; the next
                # build must reconstruct its working set behind the durable cutoff.
                working_set = _checkpoint_projection(checkpoint, session_id, user_id)
                working_set["checkpoint_generation_reset"] = True
            else:
                context = _checkpoint_context(checkpoint)
                session_state = _checkpoint_session_state(checkpoint)
                messages = _checkpoint_value(checkpoint, "messages", []) or []
                incoming_messages = _format_messages(list(messages), limit=20)
                request_id = context.get("request_id")
                prior_request_id = working_set.get("synchronized_request_id")
                working_set["recent_messages"] = _merge_checkpoint_messages(
                    list(working_set.get("recent_messages") or []),
                    incoming_messages,
                    request_id=request_id,
                    prior_request_id=prior_request_id,
                    limit=20,
                )
                working_set["synchronized_request_id"] = request_id
                working_set["session_state"] = session_state
                # Checkpoint state is authoritative. Rebuild these P1 fields rather
                # than merging paths left by a prior pending action or tool call.
                working_set["protected_fields"] = extract_protected_fields(session_state)
                working_set["last_event_seq"] = max(prior_seq, last_event_seq)
                working_set["checkpoint_version"] = checkpoint_version
                if incoming_cutoff is not None:
                    working_set["cutoff_seq"] = incoming_cutoff
                working_set.pop("requires_cold_restore", None)
                working_set.pop("checkpoint_generation_reset", None)
        _validate_working_set_owner(working_set, session_id, user_id)
        active = active_context.get()
        if active is not None and active.manager is self and active.session_id == session_id and active.user_id == user_id:
            # Even if writing the cache projection fails, the in-flight build must
            # not accept a cache cursor from before a clear/cutoff boundary.
            active.trusted_last_event_seq = last_event_seq
            active.checkpoint_version = checkpoint_version
        cache_put = getattr(self.cache, "put", None) if self.cache is not None else None
        if callable(cache_put):
            try:
                await cache_put(session_id, user_id, working_set)
            except Exception:
                self._metrics["context_cache_write_errors_total"] += 1
                return False
        self._metrics["context_checkpoint_syncs_total"] += 1
        return True

    async def invalidate_session(self, session_id: str, user_id: str) -> None:
        """Invalidate exactly the owner-scoped working-set key for a session."""
        _validate_owner(session_id, user_id)
        invalidator = getattr(self.cache, "invalidate", None) if self.cache is not None else None
        if callable(invalidator):
            await invalidator(session_id, user_id)
        self._ephemeral_events.pop((session_id, user_id), None)

    def metrics_snapshot(self) -> dict[str, int | float]:
        """Return aggregate numeric-only counters (no IDs, prompts, or labels)."""
        snapshot = dict(self._metrics)
        status = getattr(self.cache, "backend_status", {}) if self.cache is not None else {}
        if isinstance(status, dict):
            snapshot["context_real_redis_last"] = int(bool(status.get("redis_available")))
        return snapshot

    async def _load_working_set(
        self,
        session_id: str,
        user_id: str,
        *,
        recent_limit: int,
        diagnostics: dict[str, Any],
    ) -> dict[str, Any]:
        _validate_owner(session_id, user_id)
        hot: dict[str, Any] | None = None
        if self.cache is not None and callable(getattr(self.cache, "get", None)):
            try:
                hot = await self.cache.get(session_id, user_id)
            except ContextOwnershipError:
                raise
            except Exception as exc:
                self._metrics["context_cache_read_errors_total"] += 1
                diagnostics["working_set_cache_error"] = type(exc).__name__
        # Report the transport this build actually used: the lazy Redis client
        # only exists after the cache operation above touched it.
        cache_status = getattr(self.cache, "backend_status", {}) if self.cache is not None else {}
        if not isinstance(cache_status, dict):
            cache_status = {}
        diagnostics["working_set_cache_backend"] = str(cache_status.get("backend", "unavailable"))
        diagnostics["working_set_cache_is_real_redis"] = bool(cache_status.get("redis_available", False))
        ctx = active_context.get()
        trusted_seq = ctx.trusted_last_event_seq if ctx and ctx.manager is self and ctx.session_id == session_id and ctx.user_id == user_id else None
        trusted_version = ctx.checkpoint_version if ctx and ctx.manager is self and ctx.session_id == session_id and ctx.user_id == user_id else None
        stale = bool(hot and hot.get("requires_cold_restore"))
        if hot is not None and trusted_seq is not None and int(hot.get("last_event_seq") or 0) < trusted_seq:
            stale = True
        if hot is not None and trusted_version is not None and int(hot.get("checkpoint_version") or 0) < trusted_version:
            stale = True
        diagnostics["working_set_cache_hit"] = hot is not None and not stale
        diagnostics["working_set_cache_stale"] = bool(hot is not None and stale)
        if hot is not None and not stale:
            self._metrics["context_cache_hits_total"] += 1
            diagnostics["storage_read_path"] = "owner_scoped_cache"
            self._metrics["context_real_redis_last"] = int(bool(cache_status.get("redis_available", False)))
            return _normalise_working_set(hot, session_id, user_id)

        self._metrics["context_cache_misses_total"] += 1
        cold: dict[str, Any] | None = None
        start = time.perf_counter()
        loader = getattr(self.event_store, "load_working_set", None) if self.event_store is not None else None
        if callable(loader):
            cold = await loader(session_id, user_id, recent_limit=recent_limit)
            elapsed = (time.perf_counter() - start) * 1000
            diagnostics["restore_latency_ms"] = round(elapsed, 3)
            self._metrics["context_cold_restores_total"] += 1
            self._metrics["context_cold_restore_latency_ms_total"] += elapsed
        if cold is not None:
            working_set = _normalise_working_set(cold, session_id, user_id)
            working_set["checkpoint_version"] = int(cold.get("checkpoint_version") or 0)
            if trusted_seq is not None and int(working_set.get("last_event_seq") or 0) < trusted_seq:
                # A checkpoint cursor is trusted; an incomplete restore is a hard
                # consistency failure rather than an opportunity to serve stale state.
                raise ContextOwnershipError("cold working-set cursor is behind the trusted checkpoint")
            if trusted_version is not None and working_set["checkpoint_version"] < trusted_version:
                raise ContextOwnershipError("cold working-set version is behind the trusted checkpoint")
            if self.cache is not None and callable(getattr(self.cache, "put", None)):
                try:
                    await self.cache.put(session_id, user_id, working_set)
                except Exception:
                    self._metrics["context_cache_write_errors_total"] += 1
            diagnostics["storage_read_path"] = "cold_event_store_restore"
            return working_set

        diagnostics["storage_read_path"] = "ephemeral_or_empty"
        events = self._ephemeral_events.get((session_id, user_id), [])[-recent_limit:]
        return _normalise_working_set({
            "recent_messages": [],
            "recent_tool_events": [event for event in events if normalize_event_type(event.get("event_type")) in {"tool_call", "tool_result"}][-8:],
            "rolling_summary": rolling_summary_from_events(events),
            "archive_summary": archive_from_events(events) if events else {},
            "session_state": {},
            "last_event_seq": events[-1]["seq"] if events else 0,
            "version": 0,
            "checkpoint_version": 0,
            "protected_fields": _protected_from_events(events),
            "summary_event_seq": events[-1]["seq"] if events else 0,
        }, session_id, user_id)

    async def _events_range(
        self,
        session_id: str,
        user_id: str,
        *,
        after_seq: int,
        before_seq: int,
        limit: int,
    ) -> list[dict[str, Any]]:
        getter = getattr(self.event_store, "events_range", None) if self.event_store is not None else None
        if callable(getter):
            events = await getter(
                session_id,
                user_id,
                after_seq=after_seq,
                before_seq=before_seq,
                limit=limit,
            )
        elif self.event_store is None:
            events = [
                event for event in self._ephemeral_events.get((session_id, user_id), [])
                if after_seq < int(event.get("seq") or 0) <= before_seq
            ][:limit]
        else:
            # Compatibility fallback only: latest-N is usable for a cursor range
            # when it contains the next contiguous event. It must never jump gaps.
            recent_getter = getattr(self.event_store, "recent_events", None)
            if not callable(recent_getter):
                return []
            events = await recent_getter(session_id, user_id, limit=min(limit, 1000))
        ordered = sorted(
            (event for event in events if after_seq < int(event.get("seq") or 0) <= before_seq),
            key=lambda event: int(event.get("seq") or 0),
        )
        contiguous: list[dict[str, Any]] = []
        expected = after_seq + 1
        for event in ordered:
            seq = int(event.get("seq") or 0)
            if seq != expected:
                break
            contiguous.append(event)
            expected += 1
            if len(contiguous) >= limit:
                break
        return contiguous

    async def _hydrate_cold_events(
        self,
        session_id: str,
        user_id: str,
        working_set: dict[str, Any],
    ) -> list[dict[str, Any]]:
        recent_getter = getattr(self.event_store, "recent_events", None) if self.event_store is not None else None
        if not callable(recent_getter):
            return []
        last_seq = int(working_set.get("last_event_seq") or 0)
        summary_seq = int(working_set.get("summary_event_seq") or 0)
        summary_events: list[dict[str, Any]] = []
        if last_seq - summary_seq >= _COMPACTION_EVENT_GAP:
            summary_events = await self._events_range(
                session_id,
                user_id,
                after_seq=summary_seq,
                before_seq=last_seq,
                limit=_SUMMARY_EVENT_LIMIT,
            )
        recent_events = await recent_getter(session_id, user_id, limit=_RECENT_EVENT_LIMIT)
        working_set["recent_tool_events"] = [
            event for event in recent_events
            if normalize_event_type(event.get("event_type"), event.get("payload")) in {"tool_call", "tool_result"}
        ][-8:]
        _deep_merge(working_set.setdefault("protected_fields", {}), _protected_from_events(recent_events))
        _deep_merge(working_set.setdefault("protected_fields", {}), _protected_from_events(summary_events))
        return summary_events

    async def _maybe_compact_summary(
        self,
        session_id: str,
        user_id: str,
        working_set: dict[str, Any],
        diagnostics: dict[str, Any],
        *,
        available_events: list[dict[str, Any]] | None = None,
    ) -> None:
        last_seq = int(working_set.get("last_event_seq") or 0)
        summary_seq = int(working_set.get("summary_event_seq") or 0)
        if last_seq - summary_seq < _COMPACTION_EVENT_GAP:
            return
        events = list(available_events or [])
        if not events or min((int(event.get("seq") or 0) for event in events), default=last_seq + 1) != summary_seq + 1:
            events = await self._events_range(
                session_id,
                user_id,
                after_seq=summary_seq,
                before_seq=last_seq,
                limit=_SUMMARY_EVENT_LIMIT,
            )
        ordered = sorted(
            (event for event in events if summary_seq < int(event.get("seq") or 0) <= last_seq),
            key=lambda event: int(event.get("seq") or 0),
        )
        contiguous: list[dict[str, Any]] = []
        expected = summary_seq + 1
        for event in ordered:
            seq = int(event.get("seq") or 0)
            if seq != expected:
                break
            contiguous.append(event)
            expected += 1
        events = contiguous
        if not events:
            diagnostics["summary_range_gap"] = True
            self._metrics["context_summary_range_gaps_total"] += 1
            return
        prior_rolling = str(working_set.get("rolling_summary") or "")
        delta_rolling = rolling_summary_from_events(events, max_items=min(len(events), 12))
        # Append a summary delta; never feed a previous summary back into the
        # event summarizer. The structured archive retains long-term provenance.
        rolling_lines = [line for line in (prior_rolling + "\n" + delta_rolling).splitlines() if line][-12:]
        while len(rolling_lines) > 1 and len("\n".join(rolling_lines)) > 2400:
            rolling_lines.pop(0)
        working_set["rolling_summary"] = "\n".join(rolling_lines)
        incoming_archive = archive_from_events(events)
        working_set["archive_summary"] = merge_archives(working_set.get("archive_summary"), incoming_archive)
        _deep_merge(working_set.setdefault("protected_fields", {}), _protected_from_events(events))
        cursor = max(int(event.get("seq") or 0) for event in events)
        working_set["summary_event_seq"] = cursor
        working_set["last_event_seq"] = max(last_seq, cursor)
        if self.event_store is not None and callable(getattr(self.event_store, "save_digest", None)):
            await self.event_store.save_digest(
                session_id,
                user_id,
                rolling_summary=working_set["rolling_summary"],
                archive_summary=working_set["archive_summary"],
                protected_fields=working_set["protected_fields"],
                summary_event_seq=cursor,
            )
            working_set["version"] = int(working_set.get("version") or 0) + 1
            self._metrics["context_digest_saves_total"] += 1
        diagnostics["summary_event_count"] = len(events)
        diagnostics["summary_event_cursor"] = cursor
        diagnostics["summary_provenance_preserved"] = True
        self._metrics["context_compactions_total"] += 1
        if self.cache is not None and callable(getattr(self.cache, "put", None)):
            try:
                await self.cache.put(session_id, user_id, working_set)
            except Exception:
                self._metrics["context_cache_write_errors_total"] += 1

    async def _user_profile_block(
        self,
        user_id: str,
        profile: ModelProfile,
        diagnostics: dict[str, Any],
    ) -> str:
        if self.user_memory is None:
            diagnostics["user_profile_card_count"] = 0
            return ""
        getter = getattr(self.user_memory, "profile_cards", None)
        if callable(getter):
            self._metrics["context_user_profile_reads_total"] += 1
            cards = await getter(user_id, limit=profile.user_memory_top_k)
        else:
            cards = []
        card_count = len(cards or [])
        self._metrics["context_user_profile_cards_total"] += card_count
        diagnostics["user_profile_card_count"] = card_count
        return _render_json_block(cards[:profile.user_memory_top_k]) if cards else ""

    async def _retrieved_user_memory_block(
        self,
        user_id: str,
        current_message: str,
        profile: ModelProfile,
        diagnostics: dict[str, Any],
    ) -> str:
        if self.user_memory is None:
            diagnostics["retrieved_user_memory_hit"] = False
            return ""
        retrieve = getattr(self.user_memory, "retrieve", None)
        if callable(retrieve):
            self._metrics["context_retrieved_memory_queries_total"] += 1
            hits = await retrieve(user_id, current_message, top_k=profile.user_memory_top_k)
        else:
            hits = []
        hit_count = len(hits or [])
        self._metrics["context_retrieved_memory_hits_total"] += hit_count
        self._metrics["context_retrieved_memory_hit_queries_total"] += int(hit_count > 0)
        diagnostics["retrieved_user_memory_hit"] = bool(hit_count)
        return _render_json_block(hits[:profile.user_memory_top_k]) if hits else ""

    async def build(
        self,
        session_id: str,
        user_id: str,
        agent: str,
        current_message: str,
        model: str | ModelProfile | None = None,
        *,
        state: dict[str, Any] | None = None,
        system_prompt: str = "",
        task_message: str | None = None,
        evidence: Any | None = None,
        tool_schemas: Any | None = None,
    ) -> ContextPackage:
        _validate_owner(session_id, user_id)
        self._metrics["context_builds_total"] += 1
        profile = profile_from_model(model or self.default_model)
        counter = TokenCounter(profile, self.tokenizer, native=self.native_tokenizer)
        diagnostics: dict[str, Any] = {
            **counter.diagnostics,
            "agent": agent,
            "model": profile.name,
            "provider": profile.provider,
            "provider_limit_precision": "estimate",
            "max_output_tokens": profile.max_output_tokens,
            "overflow": None,
            "compression_count": 0,
            "compression_attempts": 0,
            "compression_layers": [],
        }
        try:
            active = active_context.get()
            effective_state = state
            if (
                effective_state is None
                and active is not None
                and active.manager is self
                and active.session_id == session_id
                and active.user_id == user_id
            ):
                effective_state = active.state
            policy = policy_for(agent)
            query_only = bool(policy.metadata.get("query_only"))
            cold_events: list[dict[str, Any]] = []
            if query_only:
                # Isolated query/compliance calls have no owner session to hydrate;
                # don't turn an ephemeral scope into an event-store lookup.
                working_set = _normalise_working_set({}, session_id, user_id)
                diagnostics["working_set_cache_backend"] = "not_used_query_only"
                diagnostics["working_set_cache_is_real_redis"] = False
                diagnostics["storage_read_path"] = "query_only_no_storage"
            else:
                working_set = await self._load_working_set(
                    session_id,
                    user_id,
                    recent_limit=profile.recent_messages,
                    diagnostics=diagnostics,
                )
                if diagnostics.get("storage_read_path") == "cold_event_store_restore":
                    cold_events = await self._hydrate_cold_events(session_id, user_id, working_set)
                    if self.cache is not None and callable(getattr(self.cache, "put", None)):
                        try:
                            await self.cache.put(session_id, user_id, working_set)
                        except Exception:
                            self._metrics["context_cache_write_errors_total"] += 1
                await self._maybe_compact_summary(
                    session_id, user_id, working_set, diagnostics, available_events=cold_events
                )
            recent_tool_events = list(working_set.get("recent_tool_events") or [])
            safe_session_state = _safe_session_state(working_set.get("session_state", {}))
            safe_request_state = _safe_state(effective_state or {})
            if safe_request_state:
                # Current request/checkpoint facts override the cached snapshot.
                safe_session_state.update(safe_request_state)
            pending_action_authoritative = "pending_action" in safe_session_state
            protected_fields: dict[str, Any] = {}
            if not query_only:
                if pending_action_authoritative:
                    # Once current state says a pending action exists or is null,
                    # historical tool arguments/protected paths are no longer P1.
                    protected_fields = extract_protected_fields(safe_session_state)
                    protected_fields.setdefault("pending_action", None)
                    diagnostics["pending_action_authoritative"] = True
                else:
                    for source in (
                        working_set.get("protected_fields", {}),
                        extract_protected_fields(safe_session_state),
                        _protected_from_events(recent_tool_events),
                    ):
                        _deep_merge(protected_fields, source if isinstance(source, dict) else {})

            blocks: list[ContextBlock] = []
            static_content = "\n\n".join(part for part in (system_prompt, policy.static_rules) if part)
            if static_content:
                blocks.append(ContextBlock(
                    "System", static_content, 0, 0,
                    required="System" in policy.required_blocks,
                ))
            if tool_schemas and not query_only:
                blocks.append(ContextBlock(
                    "ToolSchemas", _render_json_block(tool_schemas), 1, 1,
                    required="ToolSchemas" in policy.p1_blocks or "ToolSchemas" in policy.required_blocks,
                ))

            if policy.include_user_memory and not query_only:
                profile_text = await self._user_profile_block(user_id, profile, diagnostics)
                if profile_text:
                    blocks.append(ContextBlock("UserProfileCards", profile_text, 2, DYNAMIC_ORDER["UserProfileCards"]))
                retrieved_text = await self._retrieved_user_memory_block(
                    user_id, current_message, profile, diagnostics
                )
                if retrieved_text:
                    blocks.append(ContextBlock("RetrievedUserMemory", retrieved_text, 4, DYNAMIC_ORDER["RetrievedUserMemory"]))

            if policy.include_session_state and not query_only:
                session_state = safe_session_state
                if session_state:
                    blocks.append(ContextBlock(
                        "SessionState",
                        _render_json_block(session_state),
                        1,
                        DYNAMIC_ORDER["SessionState"],
                        required="SessionState" in policy.p1_blocks,
                    ))

            evidence_items = _bounded_evidence(evidence, profile.evidence_top_k, profile.tool_preview_chars)
            tool_previews = [
                tool_result_preview(event, max_chars=profile.tool_preview_chars)
                for event in recent_tool_events
            ][-profile.evidence_top_k:]
            if policy.include_evidence and not query_only and (evidence_items or tool_previews):
                blocks.append(ContextBlock(
                    "Evidence",
                    _render_json_block({"retrieval": evidence_items, "tool_events": tool_previews}),
                    2,
                    DYNAMIC_ORDER["Evidence"],
                ))

            has_summary = bool(working_set.get("rolling_summary") or working_set.get("archive_summary"))
            if policy.include_summary and not query_only and has_summary:
                summary_token_limit = max(128, min(600, profile.prompt_budget // 8))
                summary_parts = summary_prompt_preview(
                    working_set.get("rolling_summary"),
                    working_set.get("archive_summary"),
                    token_counter=counter,
                    max_tokens=summary_token_limit,
                )
                diagnostics["summary_prompt_token_limit"] = summary_token_limit
                blocks.append(ContextBlock("Summary", _render_json_block(summary_parts), 3, DYNAMIC_ORDER["Summary"]))

            state_messages = (effective_state or {}).get("messages") if isinstance(effective_state, dict) else None
            formatted_state = _format_messages(state_messages, limit=profile.recent_messages) if isinstance(state_messages, list) else []
            working_recent = list(working_set.get("recent_messages") or [])[-profile.recent_messages:]
            current_source_event_removed = False
            if len(formatted_state) > 1:
                # Multi-message state is the request's actual conversation snapshot.
                recent = formatted_state
            elif formatted_state:
                # Current-message-only state is supplemented from the working set.
                recent = working_recent
                ctx = active_context.get()
                request_snapshot_matches = bool(
                    ctx is not None
                    and ctx.manager is self
                    and ctx.session_id == session_id
                    and ctx.user_id == user_id
                    and working_set.get("synchronized_request_id") == ctx.request_id
                )
                if request_snapshot_matches:
                    # A checkpoint may already contain the current UserMessage
                    # followed by its assistant pair. Remove that exact last
                    # matching user event, not the final list element.
                    current_index = next(
                        (index for index in range(len(recent) - 1, -1, -1) if recent[index] == formatted_state[-1]),
                        None,
                    )
                    if current_index is not None:
                        recent.pop(current_index)
                        current_source_event_removed = True
                    else:
                        recent = [*recent, formatted_state[-1]][-profile.recent_messages:]
                else:
                    current_already_persisted = bool(
                        recent
                        and recent[-1] == formatted_state[-1]
                        and (
                            (
                                ctx is not None
                                and ctx.manager is self
                                and ctx.trusted_last_event_seq is not None
                                and int(working_set.get("last_event_seq") or 0) >= ctx.trusted_last_event_seq
                            )
                            or diagnostics.get("storage_read_path") == "cold_event_store_restore"
                        )
                    )
                    if not current_already_persisted:
                        recent = [*recent, formatted_state[-1]][-profile.recent_messages:]
            else:
                recent = working_recent
            # CurrentUser is distinct P0. Remove only the current event; identical
            # earlier confirmations/thanks remain separate history entries.
            if (
                not current_source_event_removed
                and current_message
                and recent
                and recent[-1].get("role") == "user"
                and recent[-1].get("content") == current_message
            ):
                recent = recent[:-1]
            if policy.include_recent_history and not query_only and recent:
                blocks.append(ContextBlock(
                    "RecentHistory", _render_json_block(recent), 3, DYNAMIC_ORDER["RecentHistory"]
                ))

            final_task = task_message if task_message is not None else current_message
            if "CurrentUser" in policy.required_blocks or final_task:
                blocks.append(ContextBlock(
                    "CurrentUser", final_task, 0, DYNAMIC_ORDER["CurrentUser"], required=True
                ))
            if protected_fields and not query_only:
                blocks.append(ContextBlock(
                    "ProtectedFields",
                    _render_json_block(protected_fields),
                    1,
                    DYNAMIC_ORDER["ProtectedFields"],
                    required="ProtectedFields" in policy.p1_blocks,
                ))
            if policy.include_status_bar and not query_only:
                status = {
                    "agent": agent,
                    "last_event_seq": working_set.get("last_event_seq", 0),
                }
                blocks.append(ContextBlock("StatusBar", _render_json_block(status), 4, DYNAMIC_ORDER["StatusBar"]))

            assembler = ContextAssembler(profile, counter)
            package = assembler.to_package(blocks, diagnostics=diagnostics, protected_fields=protected_fields)
            diagnostics["context_block_count"] = len(package.block_tokens)
            diagnostics["context_protected_field_count"] = len(protected_fields)
            diagnostics["context_tokenizer_warning"] = counter.diagnostics.get("tokenizer_warning")
            self._metrics["context_compactions_total"] += int(diagnostics.get("compression_count") or 0)
            self._metrics["context_final_tokens_total"] += package.total_tokens
            self._metrics["context_final_tokens_last"] = package.total_tokens
            self._metrics["context_block_tokens_total_last"] = package.total_tokens
            block_ratios = package.diagnostics.get("context_block_ratio", {})
            for block_name, metric_name in _CONTEXT_BLOCK_METRIC_NAMES.items():
                self._metrics[f"context_block_{metric_name}_tokens_last"] = int(package.block_tokens.get(block_name, 0))
                self._metrics[f"context_block_{metric_name}_ratio_last"] = float(block_ratios.get(block_name, 0.0))
            self._last_diagnostics = deepcopy(diagnostics)
            return package
        except (ContextCompressionError, ContextOverflowError):
            self._metrics["context_overflows_total"] += 1
            raise

    async def invoke(
        self,
        llm: Any,
        agent: str,
        messages: list[Any],
        *,
        state: dict[str, Any] | None = None,
        session_id: str | None = None,
        user_id: str | None = None,
        model: str | ModelProfile | None = None,
        task_message: str | None = None,
        evidence: Any | None = None,
        tool_schemas: Any | None = None,
        request_id: str | None = None,
        last_event_seq: int | None = None,
        checkpoint_version: int | None = None,
    ) -> Any:
        sid = str(session_id or (state or {}).get("session_id") or "ephemeral")
        uid = str(user_id or (state or {}).get("user_id") or "anonymous")
        _validate_owner(sid, uid)
        system_prompt, current_message = _split_messages(messages)
        active = active_context.get()
        if active is not None:
            if active.manager is not self or active.session_id != sid or active.user_id != uid:
                raise ContextOwnershipError("invoke owner differs from active request scope")
            return await self._invoke_bound(
                llm, agent, messages, state=state, system_prompt=system_prompt,
                current_message=current_message, session_id=sid, user_id=uid,
                model=model, task_message=task_message, evidence=evidence,
                tool_schemas=tool_schemas,
            )
        async with self.bind_request(
            sid, uid, request_id, state,
            last_event_seq=last_event_seq,
            checkpoint_version=checkpoint_version,
        ):
            return await self._invoke_bound(
                llm, agent, messages, state=state, system_prompt=system_prompt,
                current_message=current_message, session_id=sid, user_id=uid,
                model=model, task_message=task_message, evidence=evidence,
                tool_schemas=tool_schemas,
            )

    async def _invoke_bound(
        self,
        llm: Any,
        agent: str,
        messages: list[Any],
        *,
        state: dict[str, Any] | None,
        system_prompt: str,
        current_message: str,
        session_id: str,
        user_id: str,
        model: str | ModelProfile | None,
        task_message: str | None,
        evidence: Any | None,
        tool_schemas: Any | None,
    ) -> Any:
        original_state = state if state is not None else (active_context.get().state if active_context.get() else None)
        package = await self.build(
            session_id,
            user_id,
            agent,
            current_message,
            model,
            state=original_state,
            system_prompt=system_prompt,
            task_message=task_message,
            evidence=evidence,
            tool_schemas=tool_schemas,
        )
        profile = profile_from_model(model or self.default_model)
        active = active_context.get()
        if active is not None and active.manager is self:
            active.last_context_package = package
        self._metrics["context_invocations_total"] += 1
        return await self._invoke_llm(llm, package, profile)

    async def _invoke_llm(self, llm: Any, package: ContextPackage, profile: ModelProfile) -> Any:
        """Select a supported output-token call path before one model call."""
        binder = getattr(llm, "bind", None)
        ainvoke = getattr(llm, "ainvoke", None)
        if not callable(ainvoke):
            raise TypeError("llm must expose async ainvoke(messages)")
        if callable(binder) and _accepts_keyword(binder, "max_tokens"):
            # No exception-based retry: provider/model exceptions propagate and
            # the underlying model is never invoked a second time.
            bound = binder(max_tokens=profile.max_output_tokens)
            bound_ainvoke = getattr(bound, "ainvoke", None)
            if not callable(bound_ainvoke):
                raise TypeError("llm.bind(max_tokens=...) must return an ainvoke-capable runnable")
            package.diagnostics["max_output_tokens_honored"] = True
            response = bound_ainvoke(package.messages)
        elif _accepts_keyword(ainvoke, "max_tokens"):
            package.diagnostics["max_output_tokens_honored"] = True
            response = ainvoke(package.messages, max_tokens=profile.max_output_tokens)
        else:
            package.diagnostics["max_output_tokens_honored"] = False
            package.diagnostics["max_output_tokens_warning"] = "LLM signature does not expose max_tokens; one call will be made without it."
            response = ainvoke(package.messages)
        if inspect.isawaitable(response):
            return await response
        return response

    async def record_tool_call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return await self._record_tool_event("TOOL_CALL", {"name": name, "arguments": deepcopy(arguments)})

    async def record_tool_result(self, result: Any) -> dict[str, Any]:
        payload = result if isinstance(result, dict) else _object_to_dict(result)
        return await self._record_tool_event("TOOL_RESULT", payload)

    async def _record_tool_event(self, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        ctx = active_context.get()
        if ctx is None or ctx.manager is not self:
            session_id, user_id, request_id = "ephemeral", "anonymous", str(uuid4())
            execution_id = str(uuid4())
            counter = len(self._ephemeral_events.get((session_id, user_id), [])) + 1
        else:
            session_id, user_id, request_id = ctx.session_id, ctx.user_id, ctx.request_id
            ctx.event_counter += 1
            counter = ctx.event_counter
            execution_id = ctx.execution_id
        timestamp = datetime.now(timezone.utc).isoformat()
        event_payload = {
            **deepcopy(payload),
            "timestamp": timestamp,
            "request_id": request_id,
        }
        payload_hash = hashlib.sha256(
            json.dumps(event_payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()[:24]
        event_key = f"ctx:{execution_id}:{counter}:{payload_hash}"
        if self.event_store is not None and callable(getattr(self.event_store, "append_event", None)):
            # Durable append completes before cache refresh. Event keys identify
            # this execution attempt, not a business idempotency key.
            event = await self.event_store.append_event(
                session_id,
                user_id,
                event_type,
                event_payload,
                event_key=event_key,
            )
        else:
            bucket = self._ephemeral_events.setdefault((session_id, user_id), [])
            event = {
                "seq": len(bucket) + 1,
                "event_type": event_type,
                "payload": event_payload,
                "event_id": event_key,
                "event_key": event_key,
            }
            bucket.append(event)
        self._metrics["context_events_appended_total"] += 1
        if ctx is not None and ctx.manager is self:
            ctx.tool_events.append(deepcopy(event))
        await self._refresh_hot_after_event(session_id, user_id, event)
        return event

    async def _refresh_hot_after_event(self, session_id: str, user_id: str, event: dict[str, Any]) -> None:
        if self.cache is None or not callable(getattr(self.cache, "put", None)):
            return
        try:
            working_set = None
            if callable(getattr(self.cache, "get", None)):
                working_set = await self.cache.get(session_id, user_id)
            if working_set is None:
                # Do not cache an incomplete projection over a durable digest.
                # The next request performs the one required cold restore.
                if self.event_store is not None:
                    return
                working_set = _normalise_working_set({}, session_id, user_id)
            else:
                working_set = _normalise_working_set(working_set, session_id, user_id)
            working_set["last_event_seq"] = max(
                int(working_set.get("last_event_seq") or 0), int(event.get("seq") or 0)
            )
            protected = extract_protected_fields(event.get("payload", {}))
            _deep_merge(working_set.setdefault("protected_fields", {}), protected)
            tool_events = list(working_set.get("recent_tool_events") or [])
            tool_events.append(deepcopy(event))
            working_set["recent_tool_events"] = tool_events[-8:]
            await self.cache.put(session_id, user_id, working_set)
        except Exception:
            # Cache is non-authoritative. A failed refresh must not turn a
            # successful durable append into a failed business tool call.
            self._metrics["context_cache_write_errors_total"] += 1
            return


def _bounded_text(value: Any, max_chars: int) -> Any:
    if isinstance(value, str):
        return value if len(value) <= max_chars else value[:max(0, max_chars - 22)] + "…<preview-truncated>"
    if isinstance(value, list):
        return [_bounded_text(item, max_chars) for item in value[:3]]
    if isinstance(value, dict):
        result = {key: _bounded_text(item, max_chars) for key, item in value.items()}
        encoded = json.dumps(result, ensure_ascii=False, default=str)
        if len(encoded) <= max_chars:
            return result
        preferred = ("source", "title", "url", "score", "content", "metadata", "page_content", "citation")
        reduced = {key: result[key] for key in preferred if key in result}
        if not reduced:
            reduced = dict(list(result.items())[:5])
        encoded = json.dumps(reduced, ensure_ascii=False, default=str)
        if len(encoded) > max_chars:
            reduced = {key: _bounded_text(value, max(80, max_chars // max(1, len(reduced)))) for key, value in reduced.items()}
        return {**reduced, "preview_truncated": True}
    return value


def _bounded_evidence(evidence: Any, top_k: int, max_chars: int = 1200) -> Any:
    cleaned = drop_noise(evidence)
    if isinstance(cleaned, list):
        return [_bounded_text(item, max_chars) for item in cleaned[:top_k]]
    if isinstance(cleaned, dict):
        bounded = deepcopy(cleaned)
        for key, value in list(bounded.items()):
            if isinstance(value, list):
                bounded[key] = [_bounded_text(item, max_chars) for item in value[:top_k]]
            else:
                bounded[key] = _bounded_text(value, max_chars)
        return bounded
    return _bounded_text(cleaned, max_chars)


__all__ = ["ContextManager", "WorkingSetCache", "active_context"]
