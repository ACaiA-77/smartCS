"""Strict JSON checkpoint contract; never deserialize arbitrary Python objects."""

from __future__ import annotations

import json
from contextvars import ContextVar
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator


class CheckpointError(RuntimeError):
    """Recoverable workflow storage/execution failure, safe to expose by type."""


class CheckpointConflict(CheckpointError):
    pass


class CheckpointOwnershipError(CheckpointError):
    pass


class CheckpointUnavailable(CheckpointError):
    pass


class CheckpointCorrupt(CheckpointError):
    pass


class CheckpointMessage(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    role: Literal["user", "assistant"]
    content: str


class AgentCheckpoint(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    session_id: str = Field(min_length=1, max_length=128)
    user_id: str = Field(min_length=1, max_length=128)
    intent: str = ""
    current_stage: Literal[
        "PREPARED", "ROUTED", "EXECUTING", "GENERATED", "REVIEWED", "WAIT_CONFIRM", "FINISHED"
    ] = "PREPARED"
    status: Literal["running", "waiting", "finished"] = "running"
    messages: list[CheckpointMessage] = Field(default_factory=list)
    pending_action: dict[str, JsonValue] | None = None
    context: dict[str, JsonValue] = Field(default_factory=dict)
    version: int = Field(default=0, ge=0)
    last_event_seq: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_snapshot(self):
        # Reject NaN/Infinity as well as Pydantic's non-JSON input coercions.
        json.dumps(self.model_dump(), allow_nan=False)
        if any("\x00" in item or item != item.strip() for item in (self.session_id, self.user_id)):
            raise ValueError("invalid checkpoint identity")
        expected = {"FINISHED": "finished", "WAIT_CONFIRM": "waiting"}.get(self.current_stage, "running")
        if self.status != expected:
            raise ValueError("checkpoint stage/status mismatch")
        if self.current_stage == "WAIT_CONFIRM" and self.pending_action is None:
            raise ValueError("WAIT_CONFIRM requires a pending action")
        if self.pending_action is not None:
            from memory.session_store import ConversationState
            ConversationState.from_dict({"pending_action": self.pending_action})
        return self

    def payload(self) -> str:
        return json.dumps(self.model_dump(), ensure_ascii=False, allow_nan=False, separators=(",", ":"))


# Request-local hooks: shared agents/executor never carry another session's state.
active_checkpoint: ContextVar[Any | None] = ContextVar("active_checkpoint", default=None)
