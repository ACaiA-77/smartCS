"""POST /internal/operation_status — the authoritative outcome of a write.

Design §4: after an internal call times out or the socket drops, the harness
must NOT guess and must NOT blind-retry. It asks here, and the answer comes from
the ledger (the system of record for "did the side effect happen"), never from
an HTTP status code.

Verdicts:
  COMPLETED             ledger has a completed row — the write happened
  FAILED                ledger has a failed row — it definitively did not
  PROVABLY_NOT_EXECUTED no ledger row exists, so the operation was never
                        claimed and therefore never sent to the domain
  UNKNOWN               anything else (e.g. a claim left in_progress by a
                        crash) — the caller must keep reconciling
"""

from __future__ import annotations

import jwt as pyjwt
from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

from internal_api.auth import resolve_service_session
from internal_api.service_jwt import ServiceAuthUnavailable, decode_service_token
from internal_api.tools import _bearer_token, _error

router = APIRouter(prefix="/internal", tags=["internal"])

VERDICTS = ("COMPLETED", "FAILED", "PROVABLY_NOT_EXECUTED", "UNKNOWN")


class OperationStatusBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str = Field(min_length=1, max_length=128)
    client_request_id: str = Field(min_length=1, max_length=128)
    operation_id: str = Field(min_length=1, max_length=128)


def verdict_from_ledger(ledger_row: dict | None) -> tuple[str, str]:
    """Map a ledger row onto the design's verdict set.

    Kept as a pure function so the mapping is unit-testable without a server.
    """
    if ledger_row is None:
        # Never claimed => never sent. This is the only case where re-issuing is
        # provably safe.
        return "PROVABLY_NOT_EXECUTED", "no ledger claim for this operation"
    status = str(ledger_row.get("status") or "").lower()
    if status == "completed":
        return "COMPLETED", "ledger recorded a completed execution"
    if status == "failed":
        return "FAILED", "ledger recorded a failed execution"
    if status == "in_progress":
        # A claim with no terminal state may be a live execution or a crashed
        # one. Only reconciliation (or a human) may decide — never a retry.
        return "UNKNOWN", "ledger claim is in_progress with no terminal result"
    return "UNKNOWN", f"unrecognised ledger status: {status or 'empty'}"


@router.post("/operation_status")
async def operation_status(request: Request, body: OperationStatusBody) -> dict:
    token = _bearer_token(request)
    try:
        service = decode_service_token(token, require_business_user_id=True)
    except ServiceAuthUnavailable:
        raise _error(503, "internal_authentication_not_configured", "internal authentication not configured") from None
    except pyjwt.InvalidTokenError:
        raise _error(401, "invalid_service_authentication", "invalid service authentication") from None

    await resolve_service_session(request, service, body.session_id)

    ledger = getattr(request.app.state, "execution_ledger", None)
    if ledger is None:
        raise _error(503, "ledger_unavailable", "execution ledger unavailable")

    try:
        row = ledger.get(body.operation_id)
    except Exception:
        # An unreadable ledger is exactly the situation where guessing would be
        # dangerous.
        return {
            "status": "UNKNOWN",
            "detail": "ledger could not be read",
            "ledgerStatus": None,
            "operationId": body.operation_id,
        }

    verdict, detail = verdict_from_ledger(row)
    return {
        "status": verdict,
        "detail": detail,
        "ledgerStatus": (str(row.get("status")) if row else None),
        "operationId": body.operation_id,
        # Present so the harness can finish a request whose toolResult was lost
        # (F5) without replaying anything.
        "result": (row.get("result") if row else None),
    }
