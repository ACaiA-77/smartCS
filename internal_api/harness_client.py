"""Business Runtime -> pi-harness calls (history projection / delete / chat).

Symmetric to internal_api.service_jwt: the same shared secret, but the audience
flips because the receiver is now the harness:

    harness -> runtime : iss=smartcs-pi-harness        aud=smartcs-business-runtime
    runtime -> harness : iss=smartcs-business-runtime  aud=smartcs-pi-harness

Only used for pi sessions; legacy sessions keep their existing code path.

Phase 7 adds two things to this module:

* the cohort-rollout decision for NEW sessions (`harness_version_for_account`) —
  it lives here because it answers "which harness serves this account", and
  because `api/main.py` must not grow a second copy of that rule;
* `forward_chat`, the unified-entry hop that carries the caller's own user JWT
  verbatim (the harness re-verifies it locally and with the runtime) plus a
  service signature header naming the forwarding runtime.
"""

from __future__ import annotations

import hashlib
import os
import time

import httpx
import jwt as pyjwt

from internal_api.service_jwt import ALGORITHM, MAX_TTL_SECONDS, get_service_secret

HARNESS_ISSUER = "smartcs-business-runtime"
HARNESS_AUDIENCE = "smartcs-pi-harness"
DEFAULT_TIMEOUT_SECONDS = 10.0
#: A chat turn includes the model's own latency; the 10s internal-call budget
#: would turn a slow-but-healthy answer into a spurious 503.
DEFAULT_CHAT_TIMEOUT_SECONDS = 120.0
ROLLOUT_PERCENT_ENV = "SMARTCS_PI_ROLLOUT_PERCENT"
#: Salt/version prefix for the bucket hash. Changing it reshuffles every
#: account, so it is part of the rollout's observable contract.
BUCKET_SALT = "smartcs-pi-rollout:v1"
SERVICE_TOKEN_HEADER = "X-SmartCS-Service-Token"


class HarnessUnavailable(RuntimeError):
    """The harness is not configured or not reachable."""


def harness_base_url() -> str:
    base = os.getenv("PI_HARNESS_BASE_URL", "").strip().rstrip("/")
    if not base:
        raise HarnessUnavailable("PI_HARNESS_BASE_URL is not configured")
    if not base.startswith(("http://", "https://")):
        raise HarnessUnavailable("PI_HARNESS_BASE_URL must be an http(s) origin")
    return base


def rollout_percent() -> int:
    """The configured cohort size, 0-100. A malformed value is a hard error.

    Read per call (not cached) so tests and a rolling restart can change it,
    but validated loudly: a rollout switch that silently reads as 0 would look
    exactly like "the rollout is not happening".
    """
    raw = os.getenv(ROLLOUT_PERCENT_ENV, "0").strip()
    if raw == "":
        return 0
    try:
        percent = int(raw)
    except ValueError:
        raise ValueError(f"{ROLLOUT_PERCENT_ENV} must be an integer 0-100") from None
    if not 0 <= percent <= 100:
        raise ValueError(f"{ROLLOUT_PERCENT_ENV} must be an integer 0-100")
    return percent


def harness_version_for_account(account_id: int) -> str:
    """Which harness a NEW session of this account must be created on.

    Deterministic and process-independent: SHA-256 of a versioned prefix plus
    the account id, so the same account lands in the same bucket after a
    restart (Python's `hash()` is randomized per process and must never be
    used here). The verdict is a property of the account, never of the
    conversation: a session keeps the harness_version it was created with for
    its whole life, and nothing may re-bucket an existing session.
    """
    if type(account_id) is not int or account_id <= 0:
        raise ValueError("invalid account_id")
    percent = rollout_percent()
    if percent <= 0:
        return "legacy"
    if percent >= 100:
        return "pi"
    digest = hashlib.sha256(f"{BUCKET_SALT}:{account_id}".encode("utf-8")).digest()
    return "pi" if int.from_bytes(digest[:8], "big") % 100 < percent else "legacy"


def mint_service_token(
    *,
    account_id: int,
    business_user_id: str,
    session_id: str,
    client_request_id: str,
    ttl_seconds: int = MAX_TTL_SECONDS,
) -> str:
    if not 0 < ttl_seconds <= MAX_TTL_SECONDS:
        raise ValueError("ttl out of range")
    now = int(time.time())
    return pyjwt.encode(
        {
            "iss": HARNESS_ISSUER,
            "aud": HARNESS_AUDIENCE,
            "account_id": account_id,
            "business_user_id": business_user_id,
            "session_id": session_id,
            "client_request_id": client_request_id,
            "iat": now,
            "exp": now + ttl_seconds,
        },
        get_service_secret(),
        algorithm=ALGORITHM,
    )


async def call_harness(
    method: str,
    path: str,
    *,
    account_id: int,
    business_user_id: str,
    session_id: str,
    client_request_id: str = "history",
) -> httpx.Response:
    """Signed call to a pi-harness internal endpoint."""
    token = mint_service_token(
        account_id=account_id,
        business_user_id=business_user_id,
        session_id=session_id,
        client_request_id=client_request_id,
    )
    url = f"{harness_base_url()}{path}"
    try:
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_SECONDS) as client:
            return await client.request(
                method, url, headers={"Authorization": f"Bearer {token}"}
            )
    except httpx.HTTPError:
        raise HarnessUnavailable("pi harness unreachable") from None


async def forward_chat(
    *,
    account_id: int,
    business_user_id: str,
    session_id: str,
    client_request_id: str,
    message: str,
    user_token: str,
    timeout_seconds: float = DEFAULT_CHAT_TIMEOUT_SECONDS,
) -> httpx.Response:
    """Send one turn to the harness on the caller's behalf (unified entry).

    Two credentials travel, and they answer different questions:

    * `Authorization: Bearer <user_token>` — the caller's own public JWT,
      forwarded verbatim. The harness verifies it locally and then re-resolves
      the identity with the runtime, so this hop never asserts an identity.
    * `X-SmartCS-Service-Token` — proof that the Business Runtime forwarded the
      turn (same service secret as every other runtime -> harness call).

    An unreachable or misconfigured harness raises `HarnessUnavailable`; the
    caller decides what that means (a pi session answers 503, it never falls
    back to the legacy orchestrator).
    """
    if not isinstance(user_token, str) or not user_token:
        raise ValueError("user_token is required to forward a chat turn")
    service_token = mint_service_token(
        account_id=account_id,
        business_user_id=business_user_id,
        session_id=session_id,
        client_request_id=client_request_id,
    )
    url = f"{harness_base_url()}/api/chat"
    try:
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            return await client.post(
                url,
                headers={
                    "Authorization": f"Bearer {user_token}",
                    SERVICE_TOKEN_HEADER: f"Bearer {service_token}",
                },
                json={
                    "message": message,
                    "session_id": session_id,
                    "client_request_id": client_request_id,
                },
            )
    except httpx.HTTPError:
        raise HarnessUnavailable("pi harness unreachable") from None
