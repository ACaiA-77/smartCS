"""Typed contracts for centralized context assembly."""

from __future__ import annotations

from dataclasses import dataclass, field
import math
import os
from typing import Any, Literal


Priority = Literal[0, 1, 2, 3, 4, 5]


class ContextError(RuntimeError):
    """Base error for context assembly failures."""


class ContextOverflowError(ContextError):
    """Raised when required context cannot fit the configured model budget."""


class ContextOwnershipError(ContextError):
    """Raised when session/user ownership validation fails."""


class ContextCompressionError(ContextError):
    """Raised when bounded compression attempts cannot make context fit."""


class ContextTokenizerError(ContextError):
    """Raised when no explicit tokenizer is available for safe token accounting."""


@dataclass(frozen=True)
class ModelProfile:
    """Model-specific prompt budget and retrieval limits.

    ``context_limit`` is the model context limit. ``max_output_tokens``,
    ``reserve`` and ``safety_margin`` are subtracted from the configured hard
    context envelope before any prompt is packed.
    """

    name: str = "default"
    provider: str = "unknown"
    context_limit: int = 8192
    max_output_tokens: int = 1024
    reserve: int = 256
    safety_margin: int = 128
    soft_ratio: float = 0.70
    hard_ratio: float = 0.85
    recent_messages: int = 8
    user_memory_top_k: int = 3
    evidence_top_k: int = 3
    tool_preview_chars: int = 1200
    compression_attempts: int = 12

    def __post_init__(self) -> None:
        positive_integers = {
            "context_limit": self.context_limit,
            "max_output_tokens": self.max_output_tokens,
            "recent_messages": self.recent_messages,
            "user_memory_top_k": self.user_memory_top_k,
            "evidence_top_k": self.evidence_top_k,
            "tool_preview_chars": self.tool_preview_chars,
            "compression_attempts": self.compression_attempts,
        }
        for name, value in positive_integers.items():
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        for name, value in {"reserve": self.reserve, "safety_margin": self.safety_margin}.items():
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("name must be a non-empty string")
        if not isinstance(self.provider, str) or not self.provider.strip():
            raise ValueError("provider must be a non-empty string")
        for name, value in {"soft_ratio": self.soft_ratio, "hard_ratio": self.hard_ratio}.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 < value <= 1:
                raise ValueError(f"{name} must be a finite ratio in (0, 1]")
        if self.soft_ratio >= self.hard_ratio:
            raise ValueError("soft_ratio must be less than hard_ratio")
        if self.prompt_budget <= 0:
            raise ValueError("model limits leave no positive prompt budget")
        if self.soft_limit >= self.prompt_budget:
            raise ValueError("soft threshold must be below the hard prompt budget")

    @property
    def soft_limit(self) -> int:
        """Soft compaction threshold measured in prompt tokens."""
        return max(1, int(self.prompt_budget * self.soft_ratio))

    @property
    def hard_limit(self) -> int:
        """Hard context envelope before output and safety reservations."""
        return int(self.context_limit * self.hard_ratio)

    @property
    def prompt_budget(self) -> int:
        """Maximum counted prompt tokens after all reservations."""
        return self.hard_limit - self.max_output_tokens - self.reserve - self.safety_margin

    @classmethod
    def from_env(cls, *, model: str | None = None, provider: str | None = None) -> "ModelProfile":
        prefix = "SMARTCS_CONTEXT_"

        def _int(name: str, default: int, *, alias: str | None = None) -> int:
            value = os.getenv(prefix + name)
            if value is None and alias:
                value = os.getenv(prefix + alias)
            if value is None:
                return default
            try:
                return int(value)
            except ValueError as exc:
                raise ValueError(f"{prefix + name} must be an integer") from exc

        def _float(name: str, default: float) -> float:
            value = os.getenv(prefix + name)
            if value is None:
                return default
            try:
                return float(value)
            except ValueError as exc:
                raise ValueError(f"{prefix + name} must be a number") from exc

        return cls(
            name=model or os.getenv("MODEL_NAME", "default"),
            provider=provider or os.getenv("MODEL_PROVIDER", "unknown"),
            context_limit=_int("LIMIT", 8192, alias="CONTEXT_LIMIT"),
            max_output_tokens=_int("MAX_OUTPUT_TOKENS", 1024),
            reserve=_int("RESERVE", 256),
            safety_margin=_int("SAFETY_MARGIN", 128),
            soft_ratio=_float("SOFT_RATIO", 0.70),
            hard_ratio=_float("HARD_RATIO", 0.85),
            recent_messages=_int("RECENT_MESSAGES", 8),
            user_memory_top_k=_int("USER_MEMORY_TOP_K", 3),
            evidence_top_k=_int("EVIDENCE_TOP_K", 3),
            tool_preview_chars=_int("TOOL_PREVIEW_CHARS", 1200),
            compression_attempts=_int("COMPRESSION_ATTEMPTS", 12),
        )


@dataclass
class ContextBlock:
    name: str
    content: str
    priority: Priority
    order: int
    required: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)
    tokens: int = 0


@dataclass
class ContextPackage:
    messages: list[Any]
    total_tokens: int
    block_tokens: dict[str, int]
    diagnostics: dict[str, Any]
    protected_fields: dict[str, Any] = field(default_factory=dict)


@dataclass
class RequestContext:
    session_id: str
    user_id: str
    request_id: str
    manager: Any
    state: dict[str, Any] | None = None
    trusted_last_event_seq: int | None = None
    checkpoint_version: int | None = None
    execution_id: str = ""
    event_counter: int = 0
    tool_events: list[dict[str, Any]] = field(default_factory=list)
    last_context_package: ContextPackage | None = None

    @property
    def owner(self) -> tuple[str, str]:
        """Stable owner identity in ``(session_id, user_id)`` order."""
        return self.session_id, self.user_id


__all__ = [
    "ContextBlock",
    "ContextCompressionError",
    "ContextError",
    "ContextOverflowError",
    "ContextOwnershipError",
    "ContextPackage",
    "ContextTokenizerError",
    "ModelProfile",
    "RequestContext",
]
