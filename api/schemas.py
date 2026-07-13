"""Validated HTTP request and response schemas."""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator


SafeIdentifier = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    ),
]
ToolName = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z][A-Za-z0-9_]*$",
    ),
]


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: Annotated[str, Field(min_length=1, max_length=4000)]
    user_id: SafeIdentifier = "anonymous"
    session_id: SafeIdentifier | None = None

    @field_validator("message")
    @classmethod
    def normalize_message(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("message must not be blank")
        return normalized


class ChatResponse(BaseModel):
    response: str
    session_id: str
    intent: str
    secondary_intent: str = "unknown"
    response_mode: str = "execution"
    needs_clarification: bool = False
    compliance_passed: bool


class ToolCallRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: ToolName
    arguments: dict[str, Any] = Field(default_factory=dict)
