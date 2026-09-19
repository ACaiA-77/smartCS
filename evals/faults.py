"""Fault wrappers for eval-local MCP handlers."""

from __future__ import annotations

import asyncio
import inspect
import math
from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class FaultPlan:
    mode: str
    message: str = "injected eval failure"
    delay_seconds: float = 0.0

    def __post_init__(self) -> None:
        if self.mode not in {"raise_once", "raise_always", "delay"}:
            raise ValueError(f"unknown fault mode: {self.mode}")
        if not math.isfinite(self.delay_seconds) or self.delay_seconds < 0:
            raise ValueError("delay_seconds must be finite and non-negative")

    @classmethod
    def raise_once(cls, message: str = "injected eval failure") -> "FaultPlan":
        return cls("raise_once", message=message)

    @classmethod
    def raise_always(cls, message: str = "injected eval failure") -> "FaultPlan":
        return cls("raise_always", message=message)

    @classmethod
    def delay(cls, seconds: float) -> "FaultPlan":
        return cls("delay", delay_seconds=float(seconds))


class FaultInjectingHandler:
    """Observational async wrapper; it changes only the supplied eval handler."""

    def __init__(self, handler: Callable[..., Any], plan: FaultPlan):
        self.handler = handler
        self.plan = plan
        self.call_count = 0
        self.fault_count = 0

    async def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.call_count += 1
        inject = self.plan.mode == "raise_always" or (
            self.plan.mode == "raise_once" and self.call_count == 1
        ) or self.plan.mode == "delay"
        if inject:
            self.fault_count += 1
            if self.plan.mode in {"raise_once", "raise_always"}:
                raise RuntimeError(self.plan.message)
            await asyncio.sleep(self.plan.delay_seconds)

        result = self.handler(*args, **kwargs)
        if inspect.isawaitable(result):
            return await result
        return result
