"""POST /internal/compliance/review — final-answer compliance gate.

Contract (phase3-design.md §4):

    body  { "text", "session_id", "client_request_id", "intent_label"? }
    resp  { "verdict": "pass|sanitize|fail", "replacement": str|null,
            "rulesHit": [...], "llmReviewed": bool }

Two stages:
  1. **Rule mask — always runs.** Deterministic, sub-millisecond. It reuses the
     rule tables already defined in `agents/compliance_checker.py` (read-only
     import; that module is untouched) so there is exactly one source of truth
     for what counts as PII or a forbidden financial phrase.
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
import re

import jwt as pyjwt
from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

# Read-only reuse of the existing rule tables. Importing them (rather than
# copying) is what keeps PII/forbidden-term definitions from drifting apart.
from agents.compliance_checker import FORBIDDEN_TERMS, SENSITIVE_PATTERNS
from internal_api.auth import resolve_service_session
from internal_api.service_jwt import ServiceAuthUnavailable, decode_service_token
from internal_api.tools import _bearer_token, _error

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/internal", tags=["internal"])

#: Deterministic fallback used when the text cannot be safely rewritten.
SAFE_FALLBACK_TEXT = "抱歉，该回复未能通过合规检查，请稍后再试或转人工客服。"

_PII_LABELS = {
    "phone": "手机号",
    "id_card": "身份证号",
    "bank_card": "银行卡号",
    "email": "邮箱地址",
}


def _mask_match(match: re.Match) -> str:
    text = match.group()
    if len(text) <= 4:
        return "****"
    return text[:3] + "*" * (len(text) - 6) + text[-3:]


def mask_pii(content: str) -> str:
    """Same masking rule as `ComplianceCheckerAgent._mask_pii`."""
    masked = content
    for pattern in SENSITIVE_PATTERNS.values():
        masked = re.sub(pattern, _mask_match, masked)
    return masked


def rule_review(content: str) -> tuple[list[str], str]:
    """Return (violations, masked_text). Deterministic; no I/O, no model."""
    violations: list[str] = []

    for term in FORBIDDEN_TERMS:
        if term in content:
            violations.append(f"包含违规金融用语: '{term}'")

    for pii_type, pattern in SENSITIVE_PATTERNS.items():
        if re.search(pattern, content):
            violations.append(f"检测到PII信息泄露: {_PII_LABELS.get(pii_type, pii_type)}")

    return violations, mask_pii(content)


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
