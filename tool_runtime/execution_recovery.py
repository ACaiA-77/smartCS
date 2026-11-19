"""Crash-window reconciliation for stale write executions in the ledger."""

from __future__ import annotations

import logging
import math
import os
from datetime import datetime, timedelta, timezone
from typing import Any

from tool_runtime.execution_ledger import ExecutionLedger


logger = logging.getLogger(__name__)
_DEFAULT_STALE_AFTER_SECONDS = 60.0


class ExecutionReconciler:
    """Scan stale in-progress ledger claims so unresolved writes surface cleanly."""

    def __init__(self, ledger: ExecutionLedger) -> None:
        self.ledger = ledger

    def reconcile_key(self, idempotency_key: str) -> tuple[str, str]:
        """Inspect one stale write claim without touching unrelated claims."""
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
        """Scan claims older than the cutoff and return an audit summary."""
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
            except Exception as exc:  # one malformed row must not stop scanning
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
        tool_name = str(row.get("tool_name") or "unknown")
        return "manual_required", f"external/local write '{tool_name}' has no automatic domain proof"

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
