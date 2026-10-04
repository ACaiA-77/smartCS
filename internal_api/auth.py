"""POST /internal/auth/verify — one authoritative identity resolution for pi-harness.

Contract (phase1-design.md §6):

    request   Authorization: Bearer <Internal Service JWT>   (aud=smartcs-business-runtime)
              { "user_jwt": "<public JWT>", "session_id": "<uuid>" }
    response  { account_id, business_user_id, status, session_id, harness_version }
    errors    401 service JWT invalid / 403 user JWT invalid or account disabled
              / 404 session not owned by the account / 409 harness_version is not "pi"

Identity is derived ONLY from the public user JWT and the database. Nothing in
the request body may assert a user id; the body carries only the resource id.
"""

from __future__ import annotations

import jwt as pyjwt
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from auth.jwt import decode_token
from internal_api.service_jwt import ServiceAuthUnavailable, ServiceIdentity, decode_service_token

router = APIRouter(prefix="/internal", tags=["internal"])

_BEARER_PREFIX = "bearer"


class VerifyRequest(BaseModel):
    # extra="forbid" so a caller cannot smuggle an identity field that a later
    # refactor might start trusting.
    model_config = ConfigDict(extra="forbid")
    user_jwt: str = Field(min_length=1, max_length=4096)
    session_id: str = Field(min_length=1, max_length=128)


class VerifyResponse(BaseModel):
    account_id: int
    business_user_id: str
    status: str
    session_id: str
    harness_version: str


def _bearer_token(request: Request) -> str:
    header = request.headers.get("authorization")
    if not header:
        raise HTTPException(status_code=401, detail="service authentication required")
    parts = header.split()
    if len(parts) != 2 or parts[0].lower() != _BEARER_PREFIX or not parts[1]:
        raise HTTPException(status_code=401, detail="invalid service authentication")
    return parts[1]


async def resolve_service_session(
    request: Request, service: ServiceIdentity, session_id: str
) -> tuple[dict, dict]:
    """Shared prologue for every internal endpoint that acts on a session.

    Verifies the account is active, that its stored `business_user_id` matches
    the service token, and that the session belongs to that account. Returns
    `(account_row, session_row)`.

    Used by /internal/context, /internal/memory and /internal/compliance so the
    ownership rules live in exactly one place.
    """
    users = getattr(request.app.state, "platform_users", None)
    sessions = getattr(request.app.state, "platform_sessions", None)
    if users is None or sessions is None:
        raise HTTPException(status_code=503, detail="platform database unavailable")

    account = await users.by_id(service.account_id)
    if (
        not account
        or account.get("status") != "active"
        or not isinstance(account.get("business_user_id"), str)
        or account["business_user_id"] != service.business_user_id
    ):
        raise HTTPException(status_code=403, detail="identity mismatch")
    if service.session_id != session_id:
        raise HTTPException(status_code=401, detail="invalid service authentication")

    session = await sessions.get_owned(session_id, service.account_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    if (session.get("harness_version") or "legacy") != "pi":
        raise HTTPException(status_code=409, detail="session is not served by the pi harness")
    return account, session


@router.post("/auth/verify", response_model=VerifyResponse)
async def verify(request: Request, body: VerifyRequest) -> VerifyResponse:
    # 1) The caller must be the harness itself.
    token = _bearer_token(request)
    try:
        service = decode_service_token(token)
    except ServiceAuthUnavailable:
        raise HTTPException(status_code=503, detail="internal authentication not configured") from None
    except pyjwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="invalid service authentication") from None

    # 2) Identity comes only from the public user JWT, never from the body.
    try:
        claims = decode_token(body.user_jwt)
    except pyjwt.InvalidTokenError:
        raise HTTPException(status_code=403, detail="invalid user authentication") from None
    except ValueError:
        raise HTTPException(status_code=503, detail="authentication not configured") from None
    account_id = int(claims["sub"])

    # The harness may only speak for the session it minted the token for.
    if service.session_id != body.session_id or service.account_id != account_id:
        raise HTTPException(status_code=401, detail="invalid service authentication")

    # 3) Account must exist and be active.
    users = getattr(request.app.state, "platform_users", None)
    if users is None:
        raise HTTPException(status_code=503, detail="authentication unavailable")
    account = await users.by_id(account_id)
    if (
        not account
        or account.get("status") != "active"
        or account.get("id") != account_id
        or not isinstance(account.get("business_user_id"), str)
        or not account["business_user_id"]
    ):
        raise HTTPException(status_code=403, detail="inactive or unknown account")
    # Optional claim: present from Phase 2 onward, absent on the verify call
    # that resolves it. When present it must agree with the database.
    if service.business_user_id is not None and service.business_user_id != account["business_user_id"]:
        raise HTTPException(status_code=401, detail="invalid service authentication")

    # 4) Session ownership, then harness routing.
    sessions = getattr(request.app.state, "platform_sessions", None)
    if sessions is None:
        raise HTTPException(status_code=503, detail="session database unavailable")
    session = await sessions.get_owned(body.session_id, account_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")

    harness_version = session.get("harness_version") or "legacy"
    if harness_version != "pi":
        # A legacy session must never be routed through the Pi harness.
        raise HTTPException(status_code=409, detail="session is not served by the pi harness")

    return VerifyResponse(
        account_id=account_id,
        business_user_id=account["business_user_id"],
        status=account["status"],
        session_id=body.session_id,
        harness_version=harness_version,
    )
