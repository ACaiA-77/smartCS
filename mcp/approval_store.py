"""Durable, single-use human approval records for high-risk tool calls."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mcp.execution_ledger import canonical_arguments_hash


class ApprovalError(ValueError):
    """Base error for invalid approval operations."""

    error_code = "approval_error"


class ApprovalNotFoundError(ApprovalError):
    error_code = "approval_not_found"


class ApprovalStateError(ApprovalError):
    error_code = "approval_state_invalid"


@dataclass(frozen=True)
class ToolApproval:
    approval_id: str
    tool_name: str
    arguments_hash: str
    status: str
    requested_by: str | None
    decided_by: str | None
    decision_reason: str | None
    created_at: str
    decided_at: str | None
    consumed_at: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "approval_id": self.approval_id,
            "tool_name": self.tool_name,
            "arguments_hash": self.arguments_hash,
            "status": self.status,
            "requested_by": self.requested_by,
            "decided_by": self.decided_by,
            "decision_reason": self.decision_reason,
            "created_at": self.created_at,
            "decided_at": self.decided_at,
            "consumed_at": self.consumed_at,
        }


class ApprovalService:
    """Persist approval state in the same SQLite database as the ledger."""

    def __init__(self, db_path: str | Path | Any) -> None:
        db_path = getattr(db_path, "db_path", db_path)
        if db_path is None:
            raise ValueError("db_path is required")
        self._database = str(db_path)
        self.db_path = Path(db_path)
        if self._database != ":memory:":
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS tool_approvals (
                    approval_id TEXT PRIMARY KEY,
                    tool_name TEXT NOT NULL,
                    arguments_hash TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(
                        status IN ('pending', 'approved', 'rejected', 'consumed')
                    ),
                    requested_by TEXT,
                    decided_by TEXT,
                    decision_reason TEXT,
                    created_at TEXT NOT NULL,
                    decided_at TEXT,
                    consumed_at TEXT
                )
                """
            )

    def create_request(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        requested_by: str | None = None,
    ) -> ToolApproval:
        if not isinstance(tool_name, str) or not tool_name.strip():
            raise ValueError("tool_name must be a non-empty string")
        if not isinstance(arguments, dict):
            raise TypeError("arguments must be a mapping")
        now = self._now()
        arguments_hash = canonical_arguments_hash(arguments)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT COALESCE(MAX(CAST(SUBSTR(approval_id, 5) AS INTEGER)), 0)
                FROM tool_approvals
                WHERE approval_id LIKE 'APR-%'
                """
            ).fetchone()
            approval_id = f"APR-{int(row[0]) + 1:06d}"
            connection.execute(
                """
                INSERT INTO tool_approvals (
                    approval_id, tool_name, arguments_hash, status,
                    requested_by, created_at
                ) VALUES (?, ?, ?, 'pending', ?, ?)
                """,
                (approval_id, tool_name, arguments_hash, requested_by, now),
            )
            record = connection.execute(
                "SELECT * FROM tool_approvals WHERE approval_id = ?",
                (approval_id,),
            ).fetchone()
        return self._record(record)

    def approve(
        self,
        approval_id: str,
        decided_by: str,
        reason: str = "",
    ) -> ToolApproval:
        return self._decide(approval_id, "approved", decided_by, reason)

    def reject(
        self,
        approval_id: str,
        decided_by: str,
        reason: str = "",
    ) -> ToolApproval:
        return self._decide(approval_id, "rejected", decided_by, reason)

    def get(self, approval_id: str) -> ToolApproval | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM tool_approvals WHERE approval_id = ?",
                (approval_id,),
            ).fetchone()
        return self._record(row) if row is not None else None

    def consume(
        self,
        approval_id: str,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> bool:
        """Atomically consume a matching approved record exactly once."""
        if not isinstance(arguments, dict):
            return False
        now = self._now()
        arguments_hash = canonical_arguments_hash(arguments)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE tool_approvals
                SET status = 'consumed', consumed_at = ?
                WHERE approval_id = ?
                  AND tool_name = ?
                  AND arguments_hash = ?
                  AND status = 'approved'
                """,
                (now, approval_id, tool_name, arguments_hash),
            )
            return cursor.rowcount == 1

    def _decide(
        self,
        approval_id: str,
        status: str,
        decided_by: str,
        reason: str,
    ) -> ToolApproval:
        now = self._now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE tool_approvals
                SET status = ?, decided_by = ?, decision_reason = ?, decided_at = ?
                WHERE approval_id = ? AND status = 'pending'
                """,
                (status, decided_by, reason, now, approval_id),
            )
            if cursor.rowcount != 1:
                row = connection.execute(
                    "SELECT status FROM tool_approvals WHERE approval_id = ?",
                    (approval_id,),
                ).fetchone()
                if row is None:
                    raise ApprovalNotFoundError(f"approval not found: {approval_id}")
                raise ApprovalStateError(
                    f"cannot {status} approval {approval_id} from state {row['status']}"
                )
            row = connection.execute(
                "SELECT * FROM tool_approvals WHERE approval_id = ?",
                (approval_id,),
            ).fetchone()
        return self._record(row)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    @staticmethod
    def _record(row: sqlite3.Row) -> ToolApproval:
        return ToolApproval(**dict(row))

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()


__all__ = [
    "ApprovalError",
    "ApprovalNotFoundError",
    "ApprovalService",
    "ApprovalStateError",
    "ToolApproval",
]
