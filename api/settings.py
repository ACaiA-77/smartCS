"""Application settings loaded from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass


def _parse_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"Invalid boolean value: {value}")


def _parse_origins(value: str | None) -> tuple[str, ...]:
    raw = value or "http://localhost:3000,http://localhost:8000"
    origins = tuple(dict.fromkeys(item.strip().rstrip("/") for item in raw.split(",") if item.strip()))
    if not origins:
        raise ValueError("CORS_ALLOWED_ORIGINS must contain at least one origin")
    if "*" in origins:
        raise ValueError("CORS wildcard origin is not allowed")
    return origins


def _parse_probability(name: str, default: str) -> float:
    value = float(os.getenv(name, default))
    if not 0.0 <= value <= 1.0:
        raise ValueError(f"{name} must be between 0 and 1")
    return value


def _parse_positive_int(name: str, default: str) -> int:
    value = int(os.getenv(name, default))
    if value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _parse_positive_float(name: str, default: str) -> float:
    value = float(os.getenv(name, default))
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


@dataclass(frozen=True)
class AppSettings:
    cors_allowed_origins: tuple[str, ...]
    cors_allow_credentials: bool
    require_redis: bool
    require_rag_index: bool
    rag_min_score: float
    intent_confidence_threshold: float
    intent_candidate_margin: float
    intent_context_turns: int
    intent_entity_ttl_turns: int
    intent_format_repair_enabled: bool
    intent_prompt_version: str
    redis_unavailable_retry_seconds: float
    rag_query_rewrite_enabled: bool
    rag_llm_rerank_enabled: bool
    compliance_llm_review_enabled: bool

    @classmethod
    def from_env(cls) -> "AppSettings":
        prompt_version = os.getenv("INTENT_PROMPT_VERSION", "apple-support-v1").strip()
        if not prompt_version:
            raise ValueError("INTENT_PROMPT_VERSION must not be blank")
        return cls(
            cors_allowed_origins=_parse_origins(os.getenv("CORS_ALLOWED_ORIGINS")),
            cors_allow_credentials=_parse_bool(os.getenv("CORS_ALLOW_CREDENTIALS"), default=False),
            require_redis=_parse_bool(os.getenv("APP_REQUIRE_REDIS"), default=False),
            require_rag_index=_parse_bool(os.getenv("APP_REQUIRE_RAG_INDEX"), default=False),
            rag_min_score=float(os.getenv("RAG_MIN_SCORE", "-1.0")),
            intent_confidence_threshold=_parse_probability("INTENT_CONFIDENCE_THRESHOLD", "0.70"),
            intent_candidate_margin=_parse_probability("INTENT_CANDIDATE_MARGIN", "0.15"),
            intent_context_turns=_parse_positive_int("INTENT_CONTEXT_TURNS", "3"),
            intent_entity_ttl_turns=_parse_positive_int("INTENT_ENTITY_TTL_TURNS", "5"),
            intent_format_repair_enabled=_parse_bool(os.getenv("INTENT_FORMAT_REPAIR_ENABLED"), default=True),
            intent_prompt_version=prompt_version,
            redis_unavailable_retry_seconds=_parse_positive_float("REDIS_UNAVAILABLE_RETRY_SECONDS", "30"),
            rag_query_rewrite_enabled=_parse_bool(os.getenv("RAG_QUERY_REWRITE_ENABLED"), default=True),
            rag_llm_rerank_enabled=_parse_bool(os.getenv("RAG_LLM_RERANK_ENABLED"), default=True),
            compliance_llm_review_enabled=_parse_bool(os.getenv("COMPLIANCE_LLM_REVIEW_ENABLED"), default=True),
        )
