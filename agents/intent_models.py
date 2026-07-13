"""Apple support intent taxonomy and deterministic server-side routing contract."""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class PrimaryIntent(str, Enum):
    SECURITY = "security"
    ACTION = "action"
    QUERY = "query"
    CONSULTATION = "consultation"
    COMPLAINT = "complaint"
    UNKNOWN = "unknown"


class SecondaryIntent(str, Enum):
    PRODUCT_SUPPORT = "product_support"
    REPAIR_WARRANTY_POLICY = "repair_warranty_policy"
    RETURN_REFUND_POLICY = "return_refund_policy"
    SUBSCRIPTION_POLICY = "subscription_policy"
    ACCOUNT_GUIDANCE = "account_guidance"
    SALES_POLICY = "sales_policy"

    ORDER_QUERY = "order_query"
    REFUND_REQUEST = "refund_request"
    REPAIR_REQUEST = "repair_request"
    SUBSCRIPTION_CANCEL_REQUEST = "subscription_cancel_request"
    COMPLAINT = "complaint"
    HUMAN_ESCALATION = "human_escalation"

    ACCOUNT_SECURITY = "account_security"
    FRAUD_REPORT = "fraud_report"
    SENSITIVE_DATA = "sensitive_data"
    PROHIBITED_REQUEST = "prohibited_request"


class AgentTarget(str, Enum):
    KNOWLEDGE_RAG = "knowledge_rag"
    TICKET_HANDLER = "ticket_handler"
    COMPLIANCE_CHECKER = "compliance_checker"


class ReasonCode(str, Enum):
    EXPLICIT_POLICY_QUESTION = "explicit_policy_question"
    EXPLICIT_ACTION_REQUEST = "explicit_action_request"
    EXPLICIT_STATUS_QUERY = "explicit_status_query"
    SECURITY_RISK_DETECTED = "security_risk_detected"
    CONTEXT_FOLLOW_UP = "context_follow_up"
    AMBIGUOUS_REQUEST = "ambiguous_request"
    PARSER_FALLBACK = "parser_fallback"


PRIMARY_PRIORITY: dict[PrimaryIntent, int] = {
    PrimaryIntent.SECURITY: 0,
    PrimaryIntent.ACTION: 1,
    PrimaryIntent.QUERY: 2,
    PrimaryIntent.CONSULTATION: 3,
    PrimaryIntent.COMPLAINT: 4,
    PrimaryIntent.UNKNOWN: 5,
}


SECONDARY_RULES: dict[SecondaryIntent, tuple[PrimaryIntent, AgentTarget]] = {
    SecondaryIntent.PRODUCT_SUPPORT: (PrimaryIntent.CONSULTATION, AgentTarget.KNOWLEDGE_RAG),
    SecondaryIntent.REPAIR_WARRANTY_POLICY: (PrimaryIntent.CONSULTATION, AgentTarget.KNOWLEDGE_RAG),
    SecondaryIntent.RETURN_REFUND_POLICY: (PrimaryIntent.CONSULTATION, AgentTarget.KNOWLEDGE_RAG),
    SecondaryIntent.SUBSCRIPTION_POLICY: (PrimaryIntent.CONSULTATION, AgentTarget.KNOWLEDGE_RAG),
    SecondaryIntent.ACCOUNT_GUIDANCE: (PrimaryIntent.CONSULTATION, AgentTarget.KNOWLEDGE_RAG),
    SecondaryIntent.SALES_POLICY: (PrimaryIntent.CONSULTATION, AgentTarget.KNOWLEDGE_RAG),
    SecondaryIntent.ORDER_QUERY: (PrimaryIntent.QUERY, AgentTarget.TICKET_HANDLER),
    SecondaryIntent.REFUND_REQUEST: (PrimaryIntent.ACTION, AgentTarget.TICKET_HANDLER),
    SecondaryIntent.REPAIR_REQUEST: (PrimaryIntent.ACTION, AgentTarget.TICKET_HANDLER),
    SecondaryIntent.SUBSCRIPTION_CANCEL_REQUEST: (PrimaryIntent.ACTION, AgentTarget.TICKET_HANDLER),
    SecondaryIntent.COMPLAINT: (PrimaryIntent.COMPLAINT, AgentTarget.TICKET_HANDLER),
    SecondaryIntent.HUMAN_ESCALATION: (PrimaryIntent.ACTION, AgentTarget.TICKET_HANDLER),
    SecondaryIntent.ACCOUNT_SECURITY: (PrimaryIntent.SECURITY, AgentTarget.COMPLIANCE_CHECKER),
    SecondaryIntent.FRAUD_REPORT: (PrimaryIntent.SECURITY, AgentTarget.COMPLIANCE_CHECKER),
    SecondaryIntent.SENSITIVE_DATA: (PrimaryIntent.SECURITY, AgentTarget.COMPLIANCE_CHECKER),
    SecondaryIntent.PROHIBITED_REQUEST: (PrimaryIntent.SECURITY, AgentTarget.COMPLIANCE_CHECKER),
}


class IntentEntities(BaseModel):
    """Whitelisted, bounded entities retained across Apple support turns."""

    model_config = ConfigDict(extra="forbid")

    order_id: str | None = Field(default=None, max_length=128)
    ticket_id: str | None = Field(default=None, max_length=128)
    product: str | None = Field(default=None, max_length=128)
    device_model: str | None = Field(default=None, max_length=128)
    subscription: str | None = Field(default=None, max_length=128)
    account_issue: str | None = Field(default=None, max_length=128)
    region: str | None = Field(default=None, max_length=128)

    @field_validator("*", mode="before")
    @classmethod
    def normalize_entity_value(cls, value: Any) -> Any:
        if isinstance(value, str):
            normalized = value.strip()
            if not normalized:
                raise ValueError("entity values must not be blank")
            return normalized
        return value


class IntentCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    primary_intent: PrimaryIntent
    secondary_intent: SecondaryIntent
    confidence: float = Field(ge=0.0, le=1.0)


class IntentDecision(BaseModel):
    """Validated and server-normalized routing decision."""

    model_config = ConfigDict(extra="forbid")

    primary_intent: PrimaryIntent
    secondary_intent: SecondaryIntent
    confidence: float = Field(ge=0.0, le=1.0)
    entities: IntentEntities = Field(default_factory=IntentEntities)
    candidates: list[IntentCandidate] = Field(default_factory=list)
    reason_code: ReasonCode
    suggested_agent: AgentTarget


class _RawIntentDecision(IntentDecision):
    """Incoming LLM decision; the agent field is validated but never trusted."""


def _normalized_candidate(candidate: IntentCandidate) -> IntentCandidate:
    primary_intent, _ = SECONDARY_RULES[candidate.secondary_intent]
    return IntentCandidate(
        primary_intent=primary_intent,
        secondary_intent=candidate.secondary_intent,
        confidence=candidate.confidence,
    )


def _deduplicate_candidates(candidates: list[IntentCandidate]) -> list[IntentCandidate]:
    best_by_secondary: dict[SecondaryIntent, IntentCandidate] = {}
    for candidate in candidates:
        current = best_by_secondary.get(candidate.secondary_intent)
        if current is None or candidate.confidence > current.confidence:
            best_by_secondary[candidate.secondary_intent] = candidate
    return sorted(
        best_by_secondary.values(),
        key=lambda candidate: (
            PRIMARY_PRIORITY[candidate.primary_intent],
            -candidate.confidence,
            candidate.secondary_intent.value,
        ),
    )


def build_intent_decision(payload: dict[str, Any]) -> IntentDecision:
    """Validate LLM output, select a safe intent, and deterministically route it.

    The LLM-provided ``suggested_agent`` is schema-validated for observability but
    is discarded.  ``SECONDARY_RULES`` is the sole routing authority.
    """

    raw = _RawIntentDecision.model_validate(payload)
    primary = _normalized_candidate(
        IntentCandidate(
            primary_intent=raw.primary_intent,
            secondary_intent=raw.secondary_intent,
            confidence=raw.confidence,
        )
    )
    normalized_candidates = [_normalized_candidate(candidate) for candidate in raw.candidates]

    # Server-side priority outranks model confidence. This makes security and
    # explicit actions deterministic even when an LLM scores a consultation higher.
    ranked = sorted(
        [primary, *normalized_candidates],
        key=lambda candidate: (
            PRIMARY_PRIORITY[candidate.primary_intent],
            -candidate.confidence,
            candidate.secondary_intent.value,
        ),
    )
    selected = ranked[0]

    remaining = [
        candidate
        for candidate in [primary, *normalized_candidates]
        if candidate.secondary_intent is not selected.secondary_intent
    ]
    candidates = _deduplicate_candidates(remaining)[:3]
    normalized_primary, suggested_agent = SECONDARY_RULES[selected.secondary_intent]

    return IntentDecision(
        primary_intent=normalized_primary,
        secondary_intent=selected.secondary_intent,
        confidence=selected.confidence,
        entities=raw.entities,
        candidates=candidates,
        reason_code=raw.reason_code,
        suggested_agent=suggested_agent,
    )


