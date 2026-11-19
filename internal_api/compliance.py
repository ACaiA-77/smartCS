"""POST /internal/compliance/review — final-answer compliance gate.

Contract (phase3-design.md §4):

    body  { "text", "session_id", "client_request_id", "intent_label"? }
    resp  { "verdict": "pass|sanitize|fail", "replacement": str|null,
            "rulesHit": [...], "llmReviewed": bool }

Two stages:
  1. **Rule mask — always runs.** Deterministic, sub-millisecond. It calls
     `policies.compliance_rules`, the same module the legacy agent's rule stage
     calls, so there is exactly one source of truth for what counts as PII or a
     forbidden financial phrase.
  2. **LLM review — off by default.** Enabled only by
     `SMARTCS_COMPLIANCE_LLM_REVIEW=true`. Offline tests never enable it and
     never reach a real endpoint.

Verdict semantics:
  * `pass`     — nothing matched, text is safe as-is;
  * `sanitize` — a forbidden phrase or PII matched; `replacement` is the masked
                 text and MUST replace the message (user-visible == transcript);
  * `fail`     — the text cannot be safely masked (rule stage says blocked, or
                 the optional LLM stage rejects it); the caller substitutes a
                 deterministic safe fallback and the turn still completes.
"""

from __future__ import annotations

import logging
import os

import jwt as pyjwt
from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

# The neutral rule layer — shared with the legacy checker so PII and
# forbidden-term definitions cannot drift apart. Importing a leaf module here
# (rather than `agents.compliance_checker`) keeps this channel free of the
# agent layer's langchain dependency.
from internal_api.auth import resolve_service_session
from internal_api.service_jwt import ServiceAuthUnavailable, decode_service_token
from internal_api.tools import _bearer_token, _error
from policies.compliance_rules import mask_pii, rule_review

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/internal", tags=["internal"])

#: Deterministic fallback used when the text cannot be safely rewritten.
SAFE_FALLBACK_TEXT = "抱歉，该回复未能通过安全合规检查，请稍后再试或换个问法。"

# `mask_pii` and `rule_review` are re-exported from `policies.compliance_rules`
# so this module's public surface (see `__all__`) is unchanged.


def _llm_review_enabled() -> bool:
    return os.getenv("SMARTCS_COMPLIANCE_LLM_REVIEW", "false").strip().lower() == "true"


class ReviewBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=0, max_length=200_000)
    session_id: str = Field(min_length=1, max_length=128)
    client_request_id: str = Field(min_length=1, max_length=128)
    intent_label: str | None = Field(default=None, max_length=64)


@router.post("/compliance/review")
async def review(request: Request, body: ReviewBody) -> dict:
    token = _bearer_token(request)
    try:
        service = decode_service_token(token, require_business_user_id=True)
    except ServiceAuthUnavailable:
        raise _error(503, "internal_authentication_not_configured", "internal authentication not configured") from None
    except pyjwt.InvalidTokenError:
        raise _error(401, "invalid_service_authentication", "invalid service authentication") from None

    await resolve_service_session(request, service, body.session_id)

    text = body.text
    violations, masked = rule_review(text)
    llm_reviewed = False

    if violations:
        # Rules are mandatory and decisive for the shapes they cover: a masked
        # replacement is always produced, so PII can never leave unmasked.
        if masked == text:
            # A forbidden term matched but masking cannot neutralise it — the
            # only safe outcome is the deterministic fallback.
            return {
                "verdict": "fail",
                "replacement": SAFE_FALLBACK_TEXT,
                "rulesHit": violations,
                "llmReviewed": False,
            }
        return {
            "verdict": "sanitize",
            "replacement": masked,
            "rulesHit": violations,
            "llmReviewed": False,
        }

    if _llm_review_enabled():
        llm_reviewed = True
        verdict, replacement, extra = await _llm_stage(request, body, text)
        if verdict != "pass":
            return {
                "verdict": verdict,
                "replacement": replacement,
                "rulesHit": extra,
                "llmReviewed": True,
            }

    return {"verdict": "pass", "replacement": None, "rulesHit": [], "llmReviewed": llm_reviewed}


async def _llm_stage(request: Request, body: ReviewBody, text: str) -> tuple[str, str | None, list[str]]:
    """Optional second stage. Never enabled in offline tests.

    Any failure here degrades to `pass` for text the deterministic rules
    already cleared, so a flaky reviewer cannot take the service down.
    """
    checker = getattr(request.app.state, "compliance_checker", None)
    if checker is None:
        logger.warning("compliance LLM review requested but no checker is configured; passing rule-clean text")
        return "pass", None, []
    try:
        result = await checker.full_check(text)
    except Exception:
        logger.warning("compliance LLM review failed; falling back to the rule verdict", exc_info=True)
        return "pass", None, []

    if getattr(result, "passed", True):
        return "pass", None, []
    masked = getattr(result, "sanitized_content", "") or ""
    if masked and masked != text:
        return "sanitize", masked, list(getattr(result, "violations", []))
    return "fail", SAFE_FALLBACK_TEXT, list(getattr(result, "violations", []))


__all__ = ["router", "mask_pii", "rule_review", "SAFE_FALLBACK_TEXT"]
