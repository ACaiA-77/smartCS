"""Bounded execution policy for MCP tools.

This layer is intentionally separate from ``MCPToolServer.call_tool``.  It
adds confirmation, timeout, and small in-memory retry decisions without
changing the existing MCP/API call path.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import time
from dataclasses import dataclass
from typing import Any

from mcp.approval_store import ApprovalService
from mcp.execution_ledger import ExecutionLedger, canonical_arguments_hash
from mcp.mcp_server import MCPToolServer, ToolCallResult, ToolDefinition, customer_tool_arguments
from checkpoint.models import active_checkpoint
from context.manager import active_context


@dataclass(frozen=True)
class ToolExecutionContext:
    """Per-call authorization context; writes are unconfirmed by default."""

    confirmed: bool = False
    idempotency_key: str | None = None
    approval_id: str | None = None


@dataclass(frozen=True)
class ToolExecutionPolicy:
    """Execution limits used by :class:`ToolExecutor`."""

    timeout_seconds: float = 5.0
    max_read_attempts: int = 2


@dataclass
class ToolExecutionResult:
    """Structured result for one policy-governed tool execution."""

    tool_name: str
    success: bool
    status: str
    error_code: str | None = None
    error: str | None = None
    result: Any = None
    attempts: int = 0
    duration_ms: float = 0.0
    operation_type: str = "read"
    risk_level: str = "low"
    requires_confirmation: bool = False
    replayed: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "tool_name": self.tool_name,
            "success": self.success,
            "status": self.status,
            "error_code": self.error_code,
            "error": self.error,
            "result": self.result,
            "attempts": self.attempts,
            "duration_ms": self.duration_ms,
            "operation_type": self.operation_type,
            "risk_level": self.risk_level,
            "requires_confirmation": self.requires_confirmation,
            "replayed": self.replayed,
        }


class ToolExecutor:
    """Execute a registered tool with confirmation, timeout, and retry policy."""

    def __init__(
        self,
        server: MCPToolServer,
        policy: ToolExecutionPolicy | None = None,
        *,
        timeout_seconds: float | None = None,
        ledger: ExecutionLedger | None = None,
        approval_service: ApprovalService | None = None,
    ) -> None:
        self.server = server
        self.ledger = ledger
        self.approval_service = approval_service
        base_policy = policy or ToolExecutionPolicy()
        self.policy = (
            ToolExecutionPolicy(
                timeout_seconds=timeout_seconds,
                max_read_attempts=base_policy.max_read_attempts,
            )
            if timeout_seconds is not None
            else base_policy
        )

    async def execute(
        self,
        name: str,
        arguments: dict[str, Any],
        context: ToolExecutionContext | None = None,
    ) -> ToolExecutionResult:
        tool = self.server.get_tool(name)
        try:
            arguments = customer_tool_arguments(name, arguments)
        except PermissionError as exc:
            return ToolExecutionResult(
                tool_name=name,
                success=False,
                status="failed",
                error_code="user_identity_mismatch",
                error=str(exc),
                operation_type=tool.operation_type if tool is not None else "read",
                risk_level=tool.risk_level if tool is not None else "low",
                requires_confirmation=bool(tool and (tool.requires_confirmation or tool.operation_type == "write")),
            )
        # Authenticate before persisting a write plan or returning a cached ledger result.
        checkpoint = active_checkpoint.get()
        is_write = tool is not None and str(tool.operation_type or "read").lower() == "write"
        if checkpoint is not None and is_write:
            await checkpoint.before_write(name, arguments, context or ToolExecutionContext())

        request_context = active_context.get()
        context_manager = getattr(request_context, "manager", None)
        if context_manager is not None:
            await context_manager.record_tool_call(name, arguments)

        result = await self._execute(name, arguments, context)

        # Persist the full result before applying unresolved-outcome handling. If
        # event persistence fails after a write, the checkpoint remains fenced
        # and the execution ledger is the sole recovery authority on resume.
        if context_manager is not None:
            await context_manager.record_tool_result(result)
        if checkpoint is not None and is_write:
            checkpoint.after_write(result)
        return result

    async def _execute(
        self, name: str, arguments: dict[str, Any], context: ToolExecutionContext | None = None,
    ) -> ToolExecutionResult:
        context = context or ToolExecutionContext()
        tool = self.server.get_tool(name)
        if tool is None:
            return ToolExecutionResult(
                tool_name=name,
                success=False,
                status="failed",
                error_code="tool_not_found",
                error=f"Tool '{name}' not found",
            )

        operation_type = str(tool.operation_type or "read").lower()
        risk_level = str(tool.risk_level or "low").lower()
        high_risk = risk_level == "high"
        requires_confirmation = bool(
            tool.requires_confirmation or operation_type == "write" or high_risk
        )
        if requires_confirmation and not context.confirmed:
            return ToolExecutionResult(
                tool_name=name,
                success=False,
                status="confirmation_required",
                error_code="confirmation_required",
                error="tool execution requires confirmation",
                operation_type=operation_type,
                risk_level=tool.risk_level,
                requires_confirmation=requires_confirmation,
            )

        idempotency_key = self._normalize_idempotency_key(context.idempotency_key)
        if operation_type == "write":
            if idempotency_key is None:
                error_code = (
                    "invalid_idempotency_key"
                    if context.idempotency_key is not None
                    and not (
                        isinstance(context.idempotency_key, str)
                        and not context.idempotency_key.strip()
                    )
                    else "idempotency_key_required"
                )
                error = (
                    "idempotency key must be a non-empty string of at most 128 characters"
                    if error_code == "invalid_idempotency_key"
                    else "confirmed write requires an idempotency key"
                )
                return ToolExecutionResult(
                    tool_name=tool.name,
                    success=False,
                    status="failed",
                    error_code=error_code,
                    error=error,
                    operation_type=operation_type,
                    risk_level=tool.risk_level,
                    requires_confirmation=requires_confirmation,
                )
            if self.ledger is None:
                return ToolExecutionResult(
                    tool_name=tool.name,
                    success=False,
                    status="failed",
                    error_code="execution_ledger_required",
                    error="confirmed write requires an execution ledger",
                    operation_type=operation_type,
                    risk_level=tool.risk_level,
                    requires_confirmation=requires_confirmation,
                )
            claim = self.ledger.claim(
                idempotency_key,
                tool.name,
                canonical_arguments_hash(arguments),
                recovery_payload={
                    field: arguments[field]
                    for field in tool.recovery_fields
                    if field in arguments
                },
            )
            if claim.status == "replay":
                return self._replay(claim.record, tool)
            if claim.status == "conflict":
                return self._failure(
                    tool,
                    operation_type,
                    requires_confirmation,
                    "idempotency_conflict",
                    "idempotency key is already associated with a different request",
                    0,
                    None,
                )
            if claim.status == "in_progress":
                return self._failure(
                    tool,
                    operation_type,
                    requires_confirmation,
                    "execution_in_progress",
                    "execution for this idempotency key is already in progress",
                    0,
                    None,
                )

        if high_risk:
            if self.approval_service is None:
                if operation_type == "write" and idempotency_key is not None and self.ledger:
                    self.ledger.release(idempotency_key)
                return self._failure(
                    tool,
                    operation_type,
                    requires_confirmation,
                    "approval_service_required",
                    "high-risk tool execution requires an approval service",
                    0,
                    None,
                )
            if not isinstance(context.approval_id, str) or not context.approval_id.strip():
                if operation_type == "write" and idempotency_key is not None and self.ledger:
                    self.ledger.release(idempotency_key)
                return self._failure(
                    tool,
                    operation_type,
                    requires_confirmation,
                    "approval_required",
                    "high-risk tool execution requires an approval id",
                    0,
                    None,
                )
            if not self.approval_service.consume(context.approval_id, tool.name, arguments):
                if operation_type == "write" and idempotency_key is not None and self.ledger:
                    self.ledger.release(idempotency_key)
                return self._failure(
                    tool,
                    operation_type,
                    requires_confirmation,
                    "approval_invalid",
                    "approval is missing, mismatched, not approved, or already consumed",
                    0,
                    None,
                )
        max_attempts = self._max_attempts(tool, operation_type)
        started = time.perf_counter()
        last_error: Exception | None = None
        for attempt in range(1, max_attempts + 1):
            try:
                output = tool.handler(**arguments)
                if inspect.isawaitable(output):
                    output = await asyncio.wait_for(output, self.policy.timeout_seconds)
            except asyncio.TimeoutError as exc:
                last_error = exc
                if attempt < max_attempts:
                    continue
                result = self._failure(
                    tool,
                    operation_type,
                    requires_confirmation,
                    "timeout",
                    "tool execution timed out",
                    attempt,
                    started,
                )
                if operation_type == "write":
                    self.ledger.fail(idempotency_key, result)
                return result
            except Exception as exc:
                last_error = exc
                if attempt < max_attempts:
                    continue
                result = self._failure(
                    tool,
                    operation_type,
                    requires_confirmation,
                    "execution_error",
                    str(exc),
                    attempt,
                    started,
                )
                if operation_type == "write":
                    self.ledger.fail(idempotency_key, result)
                return result

            if isinstance(output, ToolCallResult):
                if not output.success:
                    result = self._failure(
                        tool,
                        operation_type,
                        requires_confirmation,
                        "tool_failed",
                        output.error or "tool call failed",
                        attempt,
                        started,
                    )
                    if operation_type == "write":
                        self.ledger.fail(idempotency_key, result)
                    return result
                output = output.result

            try:
                json.dumps(output, ensure_ascii=False)
            except (TypeError, ValueError, OverflowError):
                result = self._failure(
                    tool,
                    operation_type,
                    requires_confirmation,
                    "result_not_serializable",
                    "tool result is not JSON serializable",
                    attempt,
                    started,
                )
                if operation_type == "write":
                    self.ledger.fail(idempotency_key, result)
                return result

            # A business rejection is still a completed handler call.  Do not
            # retry it as though the transport had failed.
            result = ToolExecutionResult(
                tool_name=tool.name,
                success=True,
                status="completed",
                result=output,
                attempts=attempt,
                duration_ms=self._duration_ms(started),
                operation_type=operation_type,
                risk_level=tool.risk_level,
                requires_confirmation=requires_confirmation,
            )
            if operation_type == "write":
                self.ledger.complete(idempotency_key, result)
            return result

        # The loop always returns; keep a defensive result if policy changes.
        result = self._failure(
            tool,
            operation_type,
            requires_confirmation,
            "execution_error",
            str(last_error or "tool execution failed"),
            max_attempts,
            started,
        )
        if operation_type == "write":
            self.ledger.fail(idempotency_key, result)
        return result

    def _max_attempts(self, tool: ToolDefinition, operation_type: str) -> int:
        if operation_type == "write" or not tool.retryable:
            return 1
        return max(1, min(2, self.policy.max_read_attempts))

    def _failure(
        self,
        tool: ToolDefinition,
        operation_type: str,
        requires_confirmation: bool,
        error_code: str,
        error: str,
        attempts: int,
        started: float | None,
    ) -> ToolExecutionResult:
        return ToolExecutionResult(
            tool_name=tool.name,
            success=False,
            status="failed",
            error_code=error_code,
            error=error,
            attempts=attempts,
            duration_ms=self._duration_ms(started) if started is not None else 0.0,
            operation_type=operation_type,
            risk_level=tool.risk_level,
            requires_confirmation=requires_confirmation,
        )

    @staticmethod
    def _normalize_idempotency_key(key: str | None) -> str | None:
        if key is None:
            return None
        if not isinstance(key, str):
            return None
        normalized = key.strip()
        if not normalized or len(normalized) > 128 or "\x00" in normalized:
            return None
        return normalized

    @staticmethod
    def _replay(record: dict[str, Any] | None, tool: ToolDefinition) -> ToolExecutionResult:
        if not record or not record.get("result_json"):
            return ToolExecutionResult(
                tool_name=tool.name,
                success=False,
                status="failed",
                error_code="execution_ledger_error",
                error="stored execution result is missing",
                operation_type=str(tool.operation_type or "read").lower(),
                risk_level=tool.risk_level,
                requires_confirmation=True,
                replayed=True,
            )
        try:
            payload = json.loads(record["result_json"])
            payload["attempts"] = 0
            payload["replayed"] = True
            return ToolExecutionResult(**payload)
        except (TypeError, ValueError, json.JSONDecodeError):
            return ToolExecutionResult(
                tool_name=tool.name,
                success=False,
                status="failed",
                error_code="execution_ledger_error",
                error="stored execution result is invalid",
                operation_type=str(tool.operation_type or "read").lower(),
                risk_level=tool.risk_level,
                requires_confirmation=True,
                replayed=True,
            )

    @staticmethod
    def _duration_ms(started: float) -> float:
        return (time.perf_counter() - started) * 1000


__all__ = [
    "ExecutionLedger",
    "ToolExecutionContext",
    "ToolExecutionPolicy",
    "ToolExecutionResult",
    "ToolExecutor",
]
