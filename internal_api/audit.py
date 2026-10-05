"""Phase 6 observability on the Business Runtime side.

Two responsibilities, both from phase6-design.md:

1. **Trace propagation sink** (§1/§4). Every `/internal/*` request carries a W3C
   `traceparent` header (plus harness-specific id headers). A dependency mounted
   on the whole internal router parses it and records one span record per
   request into a BOUNDED in-process ring, so propagation is verifiable without
   an OTel collector — the design explicitly allows this ("若未启用则用轻量
   in-process 记录器"). When the OTel SDK is present the same attributes are
   also written onto the active span, which the app-level FastAPI
   instrumentation created and which already extracted the `traceparent` with
   the global W3C propagator. `tracing/` is deliberately NOT modified.

   The recorder can never affect a request: every step is wrapped, and a
   failure to record degrades to "no record", never to an error response.

2. **Audit ingest** (§3). `POST /internal/audit` validates the service JWT and
   the session ownership, then writes the batch into `audit_event`
   (migration 004). Idempotency comes from the UNIQUE key on `event_id`:
   re-sending a batch inserts nothing and is reported as `duplicates`.

Read access to the recorded spans (`GET /internal/trace/records`) is a
diagnostic surface, disabled unless `SMARTCS_INTERNAL_TRACE_RECORDS` is on;
plan v2 §6.8 routes "调试 trace" to exactly this kind of internal endpoint.
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from collections import deque
from typing import Any, AsyncIterator

import jwt as pyjwt
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict, Field, field_validator

from internal_api.auth import resolve_service_session
from internal_api.service_jwt import ServiceAuthUnavailable, decode_service_token
from internal_api.tools import _bearer_token, _error

router = APIRouter(prefix="/internal", tags=["internal"])

#: Design §3: batch ≤ 50.
MAX_EVENTS_PER_BATCH = 50
#: Defense in depth; the harness truncates well below this.
MAX_PAYLOAD_BYTES = 16 * 1024
#: Bounded ring — a diagnostic surface must not become a memory leak.
TRACE_RECORD_CAPACITY = 500
#: Upper bound for one page of the diagnostic read.
MAX_TRACE_RECORDS_PER_PAGE = 200

AUDIT_KINDS = ("tool_call", "tool_result")

_TRACEPARENT_RE = re.compile(r"^([0-9a-f]{2})-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$")
_ZERO_TRACE_ID = "0" * 32
_ZERO_SPAN_ID = "0" * 16

_TRACE_RECORDS: deque[dict[str, Any]] = deque(maxlen=TRACE_RECORD_CAPACITY)


# ---------------------------------------------------------------------------
# W3C trace context
# ---------------------------------------------------------------------------


def parse_traceparent(value: object) -> dict[str, Any] | None:
    """Parse a `traceparent` value; anything malformed means "no parent"."""
    if not isinstance(value, str):
        return None
    match = _TRACEPARENT_RE.match(value.strip().lower())
    if not match:
        return None
    version, trace_id, parent_span_id, flags = match.groups()
    if version == "ff":
        return None
    if trace_id == _ZERO_TRACE_ID or parent_span_id == _ZERO_SPAN_ID:
        return None
    return {
        "trace_id": trace_id,
        "parent_span_id": parent_span_id,
        "sampled": bool(int(flags, 16) & 0x01),
    }


def _new_span_id() -> str:
    return uuid.uuid4().hex[:16]


def _new_trace_id() -> str:
    return uuid.uuid4().hex


def _decode_identity(token: str) -> dict[str, Any]:
    """Best-effort claims read for span attributes — never raises."""
    try:
        service = decode_service_token(token)
    except Exception:
        return {}
    return {
        "account_id": service.account_id,
        "business_user_id": service.business_user_id,
        "session_id": service.session_id,
        "client_request_id": service.client_request_id,
    }


def _annotate_active_otel_span(attributes: dict[str, Any]) -> None:
    """Best-effort: write the same attributes onto the real OTel span.

    The FastAPI instrumentor (api.main) creates that span and the global W3C
    propagator has already made it a child of the harness span, so this only
    adds the harness-specific ids the automatic instrumentation cannot know.
    """
    try:
        from opentelemetry import trace as otel_trace
    except Exception:
        return
    try:
        span = otel_trace.get_current_span()
        if span is None or not span.is_recording():
            return
        for key, value in attributes.items():
            if value is not None:
                span.set_attribute(f"smartcs.{key}", value)
    except Exception:
        return


def trace_records(session_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    """Newest-last page of the in-process span records."""
    bounded = max(0, min(limit, MAX_TRACE_RECORDS_PER_PAGE))
    records = [item for item in _TRACE_RECORDS if session_id is None or item.get("session_id") == session_id]
    return records[-bounded:] if bounded else []


def reset_trace_records() -> None:
    """Test hook: drop everything recorded so far."""
    _TRACE_RECORDS.clear()


def trace_records_enabled() -> bool:
    return os.getenv("SMARTCS_INTERNAL_TRACE_RECORDS", "").strip().lower() in {"1", "true", "yes", "on"}


def _record_started(request: Request) -> dict[str, Any]:
    headers = request.headers
    parent = parse_traceparent(headers.get("traceparent"))
    identity = _decode_identity(_bearer_header(request))
    record: dict[str, Any] = {
        "trace_id": (parent or {}).get("trace_id") or _new_trace_id(),
        "span_id": _new_span_id(),
        "parent_span_id": (parent or {}).get("parent_span_id"),
        "sampled": (parent or {}).get("sampled", True),
        "method": request.method,
        "path": request.url.path,
        "agent_run_id": headers.get("x-smartcs-agent-run-id"),
        "tool_call_id": headers.get("x-smartcs-tool-call-id"),
        "operation_id": headers.get("x-smartcs-operation-id"),
        "started_at": time.time(),
    }
    record.update(identity)
    return record


def _bearer_header(request: Request) -> str:
    header = request.headers.get("authorization") or ""
    parts = header.split()
    return parts[1] if len(parts) == 2 else ""


async def internal_trace(request: Request, response: Response) -> AsyncIterator[None]:
    """Attach this request to the propagated trace (design §1/§4).

    Mounted as a dependency on the whole internal router. It must never change
    the outcome of a request: every branch that could raise is guarded, and the
    only visible effect is the `x-smartcs-trace-id` response header.
    """
    record: dict[str, Any] = {}
    started = time.perf_counter()
    try:
        record = _record_started(request)
    except Exception:
        record = {}
    # Echo the observed trace id. This is set BEFORE the endpoint runs: FastAPI
    # merges a dependency's injected Response headers into the final response at
    # call time, not after the generator resumes.
    if record:
        try:
            response.headers["x-smartcs-trace-id"] = str(record["trace_id"])
        except Exception:
            pass

    try:
        yield
        outcome, status_code = "ok", 200
    except HTTPException as exc:
        outcome, status_code = "error", exc.status_code
        raise
    except RequestValidationError:
        # Body validation runs after the dependency's pre-yield part, so the
        # refusal surfaces here rather than as an HTTPException.
        outcome, status_code = "error", 422
        raise
    except Exception as exc:
        outcome, status_code = "error", int(getattr(exc, "status_code", 500) or 500)
        raise
    finally:
        try:
            if record:
                record["outcome"] = outcome
                record["status_code"] = status_code
                record["duration_ms"] = round((time.perf_counter() - started) * 1000, 3)
                _TRACE_RECORDS.append(record)
                _annotate_active_otel_span(
                    {
                        k: v
                        for k, v in record.items()
                        if k
                        in {
                            "trace_id",
                            "session_id",
                            "client_request_id",
                            "agent_run_id",
                            "tool_call_id",
                            "operation_id",
                        }
                    }
                )
        except Exception:
            pass


# ---------------------------------------------------------------------------
# POST /internal/audit
# ---------------------------------------------------------------------------


class AuditEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(min_length=1, max_length=36)
    kind: str
    tool_name: str = Field(min_length=1, max_length=128)
    tool_call_id: str | None = Field(default=None, max_length=128)
    operation_id: str | None = Field(default=None, max_length=128)
    trace_id: str | None = Field(default=None, max_length=32)
    payload: Any = None

    @field_validator("kind")
    @classmethod
    def _known_kind(cls, value: str) -> str:
        if value not in AUDIT_KINDS:
            raise ValueError(f"kind must be one of {AUDIT_KINDS}")
        return value


class AuditBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(min_length=1, max_length=128)
    client_request_id: str = Field(min_length=1, max_length=128)
    events: list[AuditEvent] = Field(min_length=1, max_length=MAX_EVENTS_PER_BATCH)


def _encoded_payload(payload: Any) -> str:
    if payload is None:
        return "null"
    try:
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        return '"<unserialisable>"'


@router.post("/audit")
async def ingest_audit(request: Request, body: AuditBatch) -> dict[str, int]:
    """Idempotent batch ingest; duplicates are counted, never rewritten."""
    token = _bearer_token(request)
    try:
        service = decode_service_token(token, require_business_user_id=True)
    except ServiceAuthUnavailable:
        raise _error(503, "internal_authentication_not_configured", "internal authentication not configured") from None
    except pyjwt.InvalidTokenError:
        raise _error(401, "invalid_service_authentication", "invalid service authentication") from None

    await resolve_service_session(request, service, body.session_id)

    # The token names ONE request; a batch carries records for that request only.
    if service.client_request_id != body.client_request_id:
        raise _error(401, "invalid_service_authentication", "invalid service authentication")

    for event in body.events:
        if len(_encoded_payload(event.payload).encode("utf-8")) > MAX_PAYLOAD_BYTES:
            raise _error(413, "audit_payload_too_large", "audit payload exceeds the accepted size")

    database = getattr(request.app.state, "platform_database", None)
    if database is None:
        raise _error(503, "platform_database_unavailable", "platform database unavailable")

    inserted = 0
    duplicates = 0

    def write(_connection, cursor) -> bool:
        nonlocal inserted, duplicates
        for event in body.events:
            cursor.execute(
                """INSERT INTO audit_event
                     (event_id, session_id, client_request_id, kind, tool_name,
                      tool_call_id, operation_id, payload, trace_id)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON DUPLICATE KEY UPDATE id = id""",
                (
                    event.event_id,
                    body.session_id,
                    body.client_request_id,
                    event.kind,
                    event.tool_name,
                    event.tool_call_id,
                    event.operation_id,
                    _encoded_payload(event.payload),
                    event.trace_id,
                ),
            )
            # 1 = inserted, 0 = the idempotency key already existed (unchanged).
            if cursor.rowcount == 1:
                inserted += 1
            else:
                duplicates += 1
        return True

    await database._call(write)
    return {"inserted": inserted, "duplicates": duplicates}


# ---------------------------------------------------------------------------
# GET /internal/trace/records — gated diagnostic read
# ---------------------------------------------------------------------------


@router.get("/trace/records")
async def read_trace_records(request: Request, limit: int = 50) -> dict[str, Any]:
    if not trace_records_enabled():
        raise _error(404, "not_found", "not found")

    token = _bearer_token(request)
    try:
        service = decode_service_token(token, require_business_user_id=True)
    except ServiceAuthUnavailable:
        raise _error(503, "internal_authentication_not_configured", "internal authentication not configured") from None
    except pyjwt.InvalidTokenError:
        raise _error(401, "invalid_service_authentication", "invalid service authentication") from None

    await resolve_service_session(request, service, service.session_id)
    return {
        "session_id": service.session_id,
        "records": trace_records(service.session_id, limit),
    }
