"""Crash-window reconciliation for confirmed write executions.

Only ``refund_create`` and ``ticket_create`` have domain reads that can prove
the business effect already exists. Other stale writes remain in progress for
manual handling.
"""

from __future__ import annotations

import logging
import math
import os
from datetime import datetime, timedelta, timezone
from typing import Any

from mcp.execution_ledger import ExecutionLedger
from mcp.tool_execution import ToolExecutionResult
from refunds.service import RefundService
from tickets.service import TicketService


logger = logging.getLogger(__name__)
_DEFAULT_STALE_AFTER_SECONDS = 60.0


class ExecutionReconciler:
    """Reconcile stale ledger claims using authoritative domain evidence."""

    def __init__(
        self,
        ledger: ExecutionLedger,
        refund_service: RefundService,
        ticket_service: TicketService | None = None,
    ) -> None:
        self.ledger = ledger
        self.refund_service = refund_service
        self.ticket_service = ticket_service

    def reconcile_key(self, idempotency_key: str) -> tuple[str, str]:
        """Resume one checkpoint's stale write, without touching unrelated claims."""
        row = self.ledger.get(idempotency_key)
        if row is None or row.get("status") != "in_progress":
            return "skipped", "no unresolved claim"
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=self._resolve_stale_after_seconds(None))
        try:
            updated = self._as_utc(datetime.fromisoformat(row["updated_at"]))
        except (ValueError, TypeError):
            return "manual_required", "invalid execution timestamp"
        if updated >= cutoff:
            return "skipped", "claim is too recent to reconcile safely"
        return self._reconcile_row(row)

    def reconcile_stale(
        self,
        stale_after_seconds: float | None = None,
        now: datetime | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Reconcile claims older than the cutoff and return an audit summary."""
        stale_after_seconds = self._resolve_stale_after_seconds(stale_after_seconds)
        current = self._as_utc(now or datetime.now(timezone.utc))
        cutoff = current - timedelta(seconds=stale_after_seconds)
        rows = self.ledger.list_stale_in_progress(cutoff.isoformat(), limit)
        summary: dict[str, Any] = {
            "scanned": len(rows),
            "recovered_completed": 0,
            "released_for_retry": 0,
            "manual_required": 0,
            "skipped": 0,
            "items": [],
        }

        for row in rows:
            item = {
                "key": row.get("idempotency_key"),
                "tool": row.get("tool_name"),
                "outcome": "skipped",
                "reason": "not processed",
            }
            try:
                outcome, reason = self._reconcile_row(row)
                item["outcome"] = outcome
                item["reason"] = reason
                summary[self._summary_key(outcome)] += 1
            except Exception as exc:  # one malformed row must not stop recovery
                item["outcome"] = "skipped"
                item["reason"] = f"reconciliation error: {exc}"
                summary["skipped"] += 1
            summary["items"].append(item)

        return summary

    @staticmethod
    def _resolve_stale_after_seconds(value: float | None) -> float:
        raw_value: object = (
            os.getenv("TOOL_RECOVERY_STALE_SECONDS", "60")
            if value is None
            else value
        )
        try:
            resolved = float(raw_value)
        except (TypeError, ValueError):
            logger.warning(
                "Invalid stale recovery threshold; using %.0f seconds",
                _DEFAULT_STALE_AFTER_SECONDS,
            )
            return _DEFAULT_STALE_AFTER_SECONDS
        if not math.isfinite(resolved) or resolved <= 0:
            logger.warning(
                "Invalid stale recovery threshold; using %.0f seconds",
                _DEFAULT_STALE_AFTER_SECONDS,
            )
            return _DEFAULT_STALE_AFTER_SECONDS
        return resolved

    def _reconcile_row(self, row: dict[str, Any]) -> tuple[str, str]:
        key = row.get("idempotency_key")
        expected_updated_at = row.get("updated_at")
        tool_name = row.get("tool_name")
        if tool_name == "ticket_create":
            return self._reconcile_ticket(row)
        if tool_name != "refund_create":
            return "manual_required", "only refund_create and ticket_create have authoritative recovery"

        payload = row.get("recovery_payload")
        if not isinstance(payload, dict) or not self._has_identity(payload):
            return "manual_required", "refund recovery payload is missing order_id or user_id"

        effect = self.refund_service.find_existing_refund_effect(
            str(payload["order_id"]).strip(), str(payload["user_id"]).strip()
        )
        if effect is not None:
            result_payload = dict(effect)
            result_payload.update(
                {
                    "success": True,
                    "reason_code": "reconciled_existing_refund",
                    "refund_mode": result_payload.get("refund_mode"),
                }
            )
            result = ToolExecutionResult(
                tool_name="refund_create",
                success=True,
                status="completed",
                result=result_payload,
                attempts=0,
                operation_type="write",
                risk_level="medium",
                requires_confirmation=True,
            )
            if self.ledger.complete_stale(key, expected_updated_at, result):
                return "recovered_completed", "existing refund effect found"
            return "skipped", "stale claim changed before completion"

        if self.ledger.release_stale(key, expected_updated_at):
            return "released_for_retry", "no existing refund effect found"
        return "skipped", "stale claim changed before release"

    def _reconcile_ticket(self, row: dict[str, Any]) -> tuple[str, str]:
        key = row.get("idempotency_key")
        expected_updated_at = row.get("updated_at")
        if self.ticket_service is None:
            return "manual_required", "ticket service unavailable for recovery"
        payload = row.get("recovery_payload")
        if not isinstance(payload, dict) or not self._has_ticket_identity(payload):
            return (
                "manual_required",
                "ticket recovery payload is missing client_request_id, user_id, or request_payload_hash",
            )

        effect = self.ticket_service.find_by_client_request_id(
            str(payload["client_request_id"]).strip(),
            str(payload["user_id"]).strip(),
            str(payload["request_payload_hash"]).strip().lower(),
        )
        if effect is not None:
            if effect.get("_payload_conflict"):
                result = ToolExecutionResult(
                    tool_name="ticket_create",
                    success=True,
                    status="completed",
                    result={
                        "success": False,
                        "reason_code": "client_request_conflict",
                        "client_request_id": str(payload["client_request_id"]).strip(),
                    },
                    attempts=0,
                    operation_type="write",
                    risk_level="medium",
                    requires_confirmation=True,
                )
                if self.ledger.complete_stale(key, expected_updated_at, result):
                    return "recovered_completed", "durable ticket payload hash conflicts"
                return "skipped", "stale claim changed before conflict completion"
            result = ToolExecutionResult(
                tool_name="ticket_create",
                success=True,
                status="completed",
                result={
                    **effect,
                    "success": True,
                    "reason_code": "reconciled_existing_ticket",
                },
                attempts=0,
                operation_type="write",
                risk_level="medium",
                requires_confirmation=True,
            )
            if self.ledger.complete_stale(key, expected_updated_at, result):
                return "recovered_completed", "existing ticket effect found"
            return "skipped", "stale claim changed before completion"

        if self.ledger.release_stale(key, expected_updated_at):
            return "released_for_retry", "no existing ticket effect found"
        return "skipped", "stale claim changed before release"

    @staticmethod
    def _has_identity(payload: dict[str, Any]) -> bool:
        return bool(str(payload.get("order_id", "")).strip()) and bool(
            str(payload.get("user_id", "")).strip()
        )

    @staticmethod
    def _has_ticket_identity(payload: dict[str, Any]) -> bool:
        return bool(str(payload.get("client_request_id", "")).strip()) and bool(
            str(payload.get("user_id", "")).strip()
        ) and bool(str(payload.get("request_payload_hash", "")).strip())

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    @staticmethod
    def _summary_key(outcome: str) -> str:
        return {
            "recovered_completed": "recovered_completed",
            "released_for_retry": "released_for_retry",
            "manual_required": "manual_required",
        }.get(outcome, "skipped")


__all__ = ["ExecutionReconciler"]
