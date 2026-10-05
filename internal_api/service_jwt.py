"""Internal Service JWT: the pi-harness -> Business Runtime credential.

Separate secret and audience from the public user token (AUTH_JWT_SECRET /
smartcs). Never falls back to a default secret; a missing or weak secret is a
hard failure, so a misconfigured deployment cannot silently accept forged
service calls.

Claim contract (plan v2 §7):
    aud=smartcs-business-runtime · account_id · business_user_id · session_id
    · client_request_id · iat · exp        TTL <= 60s

`business_user_id` is OPTIONAL for /internal/auth/verify — that call is what
*resolves* it, so the harness cannot put it in the token yet — but REQUIRED for
every other internal endpoint (Phase 2 D5: tool calls must carry the resolved
identity so the runtime can force-bind it). Callers choose via
`require_business_user_id`.

One exception: endpoints that belong to no turn at all (Phase 11 readiness,
`/internal/ready`) verify the same secret/audience/issuer without the turn
claims — see `decode_ops_service_token`.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass

import jwt as pyjwt


ALGORITHM = "HS256"
AUDIENCE = "smartcs-business-runtime"
ISSUER = "smartcs-pi-harness"
MAX_TTL_SECONDS = 60
MIN_SECRET_BYTES = 32


class ServiceAuthUnavailable(RuntimeError):
    """The service credential is not configured or misconfigured (deployment error)."""


@dataclass(frozen=True)
class ServiceIdentity:
    account_id: int
    session_id: str
    client_request_id: str
    business_user_id: str | None = None


def get_service_secret() -> str:
    secret = os.getenv("INTERNAL_SERVICE_JWT_SECRET", "")
    if len(secret.encode("utf-8")) < MIN_SECRET_BYTES or secret != secret.strip():
        raise ServiceAuthUnavailable(
            f"INTERNAL_SERVICE_JWT_SECRET must contain at least {MIN_SECRET_BYTES} bytes "
            "without surrounding whitespace"
        )
    return secret


def _positive_int(value: object) -> bool:
    return type(value) is int and 0 < value <= 2**63 - 1


def decode_ops_service_token(token: str) -> None:
    """Verify a peer-service token that carries NO turn identity.

    `/internal/ready` (Phase 11 §4.2) is not part of a turn: nobody is asking on
    behalf of a user, so there is no `account_id` / `session_id` to bind. It is
    still not a public endpoint — the caller must prove it holds the same
    internal service secret, audience and issuer every other internal call uses.
    Inventing a fake turn identity just to satisfy `decode_service_token` would
    be worse than naming the case: this is the deployment's own peer service.

    Raises pyjwt.InvalidTokenError for anything invalid; ServiceAuthUnavailable
    when the deployment secret itself is unusable.
    """
    secret = get_service_secret()
    if not isinstance(token, str) or not 1 <= len(token) <= 4096:
        raise pyjwt.InvalidTokenError("invalid service token")
    claims = pyjwt.decode(
        token,
        secret,
        algorithms=[ALGORITHM],
        audience=AUDIENCE,
        issuer=ISSUER,
        options={"require": ["aud", "iss", "iat", "exp"]},
    )
    iat, exp = claims.get("iat"), claims.get("exp")
    if type(iat) is not int or type(exp) is not int:
        raise pyjwt.InvalidTokenError("invalid service token timestamps")
    if not 0 < exp - iat <= MAX_TTL_SECONDS:
        raise pyjwt.InvalidTokenError("service token ttl out of range")
    if exp < int(time.time()):
        raise pyjwt.InvalidTokenError("service token expired")


def decode_service_token(token: str, *, require_business_user_id: bool = False) -> ServiceIdentity:
    """Verify signature/claims and return the caller identity.

    `require_business_user_id=True` is the Phase 2 default for tool calls: a
    token that does not name the end user is refused outright rather than
    silently falling back to an unbound identity.

    Raises pyjwt.InvalidTokenError for anything invalid; ServiceAuthUnavailable
    when the deployment secret itself is unusable.
    """
    secret = get_service_secret()
    if not isinstance(token, str) or not 1 <= len(token) <= 4096:
        raise pyjwt.InvalidTokenError("invalid service token")
    claims = pyjwt.decode(
        token,
        secret,
        algorithms=[ALGORITHM],
        audience=AUDIENCE,
        issuer=ISSUER,
        options={"require": ["aud", "iss", "iat", "exp", "account_id", "session_id"]},
    )

    iat, exp = claims.get("iat"), claims.get("exp")
    if type(iat) is not int or type(exp) is not int:
        raise pyjwt.InvalidTokenError("invalid service token timestamps")
    if not 0 < exp - iat <= MAX_TTL_SECONDS:
        raise pyjwt.InvalidTokenError("service token ttl out of range")
    if exp < int(time.time()):
        raise pyjwt.InvalidTokenError("service token expired")

    account_id = claims.get("account_id")
    business_user_id = claims.get("business_user_id")
    session_id = claims.get("session_id")
    client_request_id = claims.get("client_request_id")
    if (
        not _positive_int(account_id)
        or not isinstance(session_id, str)
        or not 1 <= len(session_id) <= 128
        or not isinstance(client_request_id, str)
        or not 1 <= len(client_request_id) <= 128
        or (
            business_user_id is not None
            and (not isinstance(business_user_id, str) or not 1 <= len(business_user_id) <= 128)
        )
    ):
        raise pyjwt.InvalidTokenError("invalid service token claims")
    if require_business_user_id and business_user_id is None:
        # Phase 2 (D5): tool calls must carry the resolved end-user identity.
        raise pyjwt.InvalidTokenError("service token is missing business_user_id")

    return ServiceIdentity(
        account_id=int(account_id),
        session_id=session_id,
        client_request_id=client_request_id,
        business_user_id=business_user_id,
    )
