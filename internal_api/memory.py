"""POST /internal/memory/enqueue — durable memory outbox consumer.

Contract (phase3-design.md §3):

    body  { "session_id", "client_request_id", "source_event_id" }
    resp  { "enqueued": bool, "result": {...} }

Pipeline position: the harness records the raw user message in
`memory_source_event` *before* any model activity (Phase 1), marks the receipt
`pending`, and a dispatcher later calls this endpoint. A crash in between stays
recoverable because the ledger row is already durable.

--------------------------------------------------------------------------------
DEVIATION — reported in PHASE3_REPORT.md §6 D1, needs adjudication
--------------------------------------------------------------------------------
The design asks for a pure passthrough onto `UserMemoryService.process_message`
with the provenance check "一行不改". Implemented literally, the outbox can
never deliver for a Pi session, because that check does not read the ledger the
plan designates:

  * plan v2 §6.6 designates `memory_source_event` as the durable provenance for
    this path, and Phase 1 created it for exactly that purpose;
  * `memory/MySQLUserMemoryRepository.source_event()` instead reads
    `conversation_event` JOIN `session_digest` (the legacy checkpoint log) and
    requires a numeric `event_id`. Nothing in the Pi path writes those tables,
    so the lookup always returns None and `process_message` raises
    `MemoryProvenanceError`.

`LedgerProvenanceRepository` closes that gap from inside the approved write
scope: it delegates everything (candidate inserts, profile application, claims)
to the real repository and overrides only `source_event`, pointing it at the
ledger the plan designates. Every original guarantee is preserved — owner,
session and event must all match, the row must be uncleared, and the text is
read from the database rather than from the caller. `memory/` is untouched.

If the planner would rather teach `memory/` about the ledger (a small reviewed
change to a currently read-only module), delete this adapter and revert
`memory.py` to a pure passthrough.
"""

from __future__ import annotations

import jwt as pyjwt
from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

from internal_api.auth import resolve_service_session
from internal_api.service_jwt import ServiceAuthUnavailable, decode_service_token
from internal_api.tools import _bearer_token, _error

router = APIRouter(prefix="/internal", tags=["internal"])


class EnqueueBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str = Field(min_length=1, max_length=128)
    client_request_id: str = Field(min_length=1, max_length=128)
    source_event_id: str = Field(min_length=1, max_length=64)


async def _read_ledger_row(database, user_id: str, session_id: str, event_id: str) -> dict | None:
    """Owner/session/event scoped read of an uncleared USER_MESSAGE ledger row.

    Fails closed on every mismatch: an absent row, a wrong owner, a wrong
    session or an already-cleared row all yield None, which the caller turns
    into a hard error rather than a silent enqueue.
    """

    def read(_connection, cursor):
        cursor.execute(
            """SELECT event_id, session_id, business_user_id, content, cleared_at, seq, created_at
               FROM memory_source_event
               WHERE event_id=%s AND session_id=%s AND business_user_id=%s
               LIMIT 1""",
            (event_id, session_id, user_id),
        )
        row = cursor.fetchone()
        if not row or row["cleared_at"] is not None:
            return None
        return {
            "user_id": str(row["business_user_id"]),
            "session_id": row["session_id"],
            "event_id": str(row["event_id"]),
            "event_type": "USER_MESSAGE",
            "content": row["content"],
            # `process_message` stores these on every candidate as
            # source_seq / source_created_at, so the provenance row must carry
            # them. The ledger's AUTO_INCREMENT gives arrival order.
            "seq": int(row["seq"]),
            "created_at": row["created_at"],
        }

    return await database._call(read)


class LedgerProvenanceRepository:
    """`UserMemoryRepository` adapter backed by `memory_source_event`."""

    def __init__(self, database, delegate):
        self.db = database
        self._delegate = delegate

    def __getattr__(self, name):
        # initialize / profile cards / candidate writes / claims all keep using
        # the real repository, so application semantics are untouched.
        return getattr(self._delegate, name)

    async def source_event(self, user_id: str, session_id: str, event_id: str):
        return await _read_ledger_row(self.db, user_id, session_id, event_id)


def _ledger_database(request: Request):
    """The platform database handle the existing memory repository already uses."""
    memory = getattr(request.app.state, "user_memory_service", None)
    database = getattr(getattr(memory, "repository", None), "db", None)
    if database is not None:
        return database

    from platform_db.database import PlatformDatabase

    return PlatformDatabase.from_env()


def _service_with_ledger_provenance(request: Request):
    """Wrap the app's memory service so provenance reads the Phase 1 ledger."""
    from memory.user_memory import UserMemoryService

    memory = getattr(request.app.state, "user_memory_service", None)
    repository = getattr(memory, "repository", None)
    if memory is None or repository is None:
        return None
    if isinstance(repository, LedgerProvenanceRepository):
        return memory
    return UserMemoryService(
        repository=LedgerProvenanceRepository(_ledger_database(request), repository),
        extractor=memory.extractor,
        policy=memory.policy,
    )


@router.post("/memory/enqueue")
async def enqueue(request: Request, body: EnqueueBody) -> dict:
    token = _bearer_token(request)
    try:
        service = decode_service_token(token, require_business_user_id=True)
    except ServiceAuthUnavailable:
        raise _error(503, "internal_authentication_not_configured", "internal authentication not configured") from None
    except pyjwt.InvalidTokenError:
        raise _error(401, "invalid_service_authentication", "invalid service authentication") from None

    await resolve_service_session(request, service, body.session_id)

    memory = _service_with_ledger_provenance(request)
    if memory is None:
        raise _error(503, "memory_service_unavailable", "user memory service unavailable")

    # The text is read back from the durable ledger; the caller cannot supply it.
    row = await _read_ledger_row(
        _ledger_database(request), service.business_user_id, body.session_id, body.source_event_id
    )
    if row is None:
        raise _error(404, "source_event_not_found", "no durable provenance row for this request")

    try:
        # `process_message` re-verifies provenance through the same adapter and
        # is the only thing that applies memory policy; do not pre-empt it.
        result = await memory.process_message(
            user_id=service.business_user_id,
            session_id=body.session_id,
            event_id=body.source_event_id,
            content=row["content"],
        )
    except PermissionError as exc:
        raise _error(403, "memory_provenance_rejected", str(exc)) from None
    except ValueError as exc:
        raise _error(400, "invalid_memory_request", str(exc)) from None

    payload = result if isinstance(result, dict) else {"value": result}
    return {"enqueued": bool(payload.get("enqueued", True)), "result": payload}
