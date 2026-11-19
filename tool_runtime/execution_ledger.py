"""SQLite-backed idempotency ledger for confirmed write executions."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


class LedgerClaimLost(RuntimeError):
    """A recovery worker changed or released the claim before execution."""


def canonical_arguments_hash(arguments: dict[str, Any]) -> str:
    """Return the stable SHA-256 hash used to identify a request payload."""
    payload = json.dumps(
        arguments,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True)
class LedgerClaim:
    """Outcome of an atomic idempotency claim."""

    status: str
    record: dict[str, Any] | None = None

    @property
    def error_code(self) -> str | None:
        return {
            "conflict": "idempotency_conflict",
            "in_progress": "execution_in_progress",
        }.get(self.status)

    @property
    def replayed(self) -> bool:
        return self.status == "replay"


class ExecutionLedger:
    """Persist write execution state in the supplied SQLite database."""

    def __init__(self, db_path: str | Path) -> None:
        if db_path is None:
            raise ValueError("db_path is required")
        db_path = getattr(db_path, "db_path", db_path)
        self.db_path = Path(db_path)
        if str(self.db_path) != ":memory:":
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS tool_executions (
                    idempotency_key TEXT PRIMARY KEY,
                    tool_name TEXT NOT NULL,
                    arguments_hash TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('in_progress', 'completed', 'failed')),
                    result_json TEXT,
                    error_code TEXT,
                    error TEXT,
                    operation_type TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    recovery_payload_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(tool_executions)").fetchall()
            }
            if "recovery_payload_json" not in columns:
                connection.execute(
                    "ALTER TABLE tool_executions ADD COLUMN recovery_payload_json TEXT"
                )

    def claim(
        self,
        idempotency_key: str,
        tool_name: str,
        arguments_hash: str | dict[str, Any],
        recovery_payload: dict[str, Any] | None = None,
    ) -> LedgerClaim:
        """Atomically claim a new key or classify its existing execution."""
        if not isinstance(arguments_hash, str):
            arguments_hash = canonical_arguments_hash(arguments_hash)
        recovery_payload_json = None
        if recovery_payload is not None:
            recovery_payload_json = json.dumps(
                recovery_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        now = self._now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM tool_executions WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if row is None:
                connection.execute(
                    """
                    INSERT INTO tool_executions (
                        idempotency_key, tool_name, arguments_hash, status,
                        recovery_payload_json, created_at, updated_at
                    ) VALUES (?, ?, ?, 'in_progress', ?, ?, ?)
                    """,
                    (
                        idempotency_key,
                        tool_name,
                        arguments_hash,
                        recovery_payload_json,
                        now,
                        now,
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM tool_executions WHERE idempotency_key = ?",
                    (idempotency_key,),
                ).fetchone()
                return LedgerClaim("claimed", self._record(row))

            record = self._record(row)
            if (
                record["tool_name"] != tool_name
                or record["arguments_hash"] != arguments_hash
            ):
                return LedgerClaim("conflict", record)
            if record["status"] == "in_progress":
                return LedgerClaim("in_progress", record)
            return LedgerClaim("replay", record)

    def complete(
        self, idempotency_key: str, result: Any, *,
        connection: sqlite3.Connection | None = None,
        expected_claim: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Store a successful or business-completed execution result."""
        return self._finish(idempotency_key, "completed", result, connection, expected_claim)

    def fail(
        self, idempotency_key: str, result: Any, *,
        expected_claim: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Store a final failed execution result."""
        return self._finish(idempotency_key, "failed", result, expected_claim=expected_claim)

    @contextmanager
    def atomic_write(
        self, idempotency_key: str, expected_claim: dict[str, Any], business_db: str | Path,
    ) -> Iterator[sqlite3.Connection]:
        """Lock the claimed execution and business changes in the same database."""
        if (
            str(self.db_path) == ":memory:"
            or str(business_db) == ":memory:"
            or self.db_path.resolve() != Path(business_db).resolve()
        ):
            raise ValueError("atomic write requires the ledger and business database to match")
        connection = self._connect()
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("BEGIN IMMEDIATE")
            self._require_claim(connection, idempotency_key, expected_claim)
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _require_claim(
        connection: sqlite3.Connection, idempotency_key: str, expected: dict[str, Any],
    ) -> None:
        row = connection.execute(
            "SELECT * FROM tool_executions WHERE idempotency_key = ?", (idempotency_key,),
        ).fetchone()
        if row is None or row["status"] != "in_progress" or any(
            row[field] != expected[field]
            for field in ("tool_name", "arguments_hash", "created_at", "updated_at")
        ):
            raise LedgerClaimLost("execution claim changed before atomic write")

    def get(self, idempotency_key: str) -> dict[str, Any] | None:
        """Return one ledger row, including its decoded result when present."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM tool_executions WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
        return self._record(row) if row is not None else None

    def release(self, idempotency_key: str) -> bool:
        """Release a claim that was rejected before the handler ran."""
        with self._connect() as connection:
            cursor = connection.execute(
                """
                DELETE FROM tool_executions
                WHERE idempotency_key = ? AND status = 'in_progress'
                """,
                (idempotency_key,),
            )
        return cursor.rowcount == 1

    def list_stale_in_progress(
        self, stale_before: str | datetime, limit: int = 100
    ) -> list[dict[str, Any]]:
        """List claims older than the recovery cutoff without changing them."""
        if isinstance(stale_before, datetime):
            if stale_before.tzinfo is None:
                stale_before = stale_before.replace(tzinfo=timezone.utc)
            stale_before = stale_before.astimezone(timezone.utc).isoformat()
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT idempotency_key, tool_name, arguments_hash,
                       recovery_payload_json, created_at, updated_at
                FROM tool_executions
                WHERE status = 'in_progress' AND updated_at < ?
                ORDER BY updated_at, idempotency_key
                LIMIT ?
                """,
                (stale_before, max(1, int(limit))),
            ).fetchall()
        records = []
        for row in rows:
            record = dict(row)
            raw_payload = record.pop("recovery_payload_json", None)
            try:
                record["recovery_payload"] = (
                    json.loads(raw_payload) if raw_payload is not None else None
                )
            except (TypeError, ValueError, json.JSONDecodeError):
                record["recovery_payload"] = None
            records.append(record)
        return records

    def complete_stale(
        self,
        idempotency_key: str,
        expected_updated_at: str,
        result: Any,
    ) -> bool:
        """Complete only the exact stale claim observed by a reconciler."""
        if hasattr(result, "as_dict"):
            result = result.as_dict()
        if not isinstance(result, dict):
            raise TypeError("ledger result must be a mapping")
        result_json = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
        now = self._now()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE tool_executions
                SET status = 'completed', result_json = ?, error_code = ?, error = ?,
                    operation_type = ?, attempts = ?, updated_at = ?
                WHERE idempotency_key = ? AND status = 'in_progress'
                  AND updated_at = ?
                """,
                (
                    result_json,
                    result.get("error_code"),
                    result.get("error"),
                    result.get("operation_type"),
                    int(result.get("attempts", 0)),
                    now,
                    idempotency_key,
                    expected_updated_at,
                ),
            )
        return cursor.rowcount == 1

    def release_stale(self, idempotency_key: str, expected_updated_at: str) -> bool:
        """Release only the exact stale claim observed by a reconciler."""
        with self._connect() as connection:
            cursor = connection.execute(
                """
                DELETE FROM tool_executions
                WHERE idempotency_key = ? AND status = 'in_progress'
                  AND updated_at = ?
                """,
                (idempotency_key, expected_updated_at),
            )
        return cursor.rowcount == 1

    def _finish(
        self, idempotency_key: str, status: str, result: Any,
        connection: sqlite3.Connection | None = None,
        expected_claim: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if connection is None:
            with self._connect() as owned_connection:
                if expected_claim is not None:
                    owned_connection.execute("BEGIN IMMEDIATE")
                return self._finish(idempotency_key, status, result, owned_connection, expected_claim)
        if hasattr(result, "as_dict"):
            result = result.as_dict()
        if not isinstance(result, dict):
            raise TypeError("ledger result must be a mapping")
        result_json = json.dumps(
            result,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        now = self._now()
        if expected_claim is not None:
            self._require_claim(connection, idempotency_key, expected_claim)
        cursor = connection.execute(
            """
            UPDATE tool_executions
            SET status = ?, result_json = ?, error_code = ?, error = ?,
                operation_type = ?, attempts = ?, updated_at = ?
            WHERE idempotency_key = ?
            """,
            (
                status,
                result_json,
                result.get("error_code"),
                result.get("error"),
                result.get("operation_type"),
                int(result.get("attempts", 0)),
                now,
                idempotency_key,
            ),
        )
        if cursor.rowcount != 1:
            raise KeyError(f"unknown idempotency key: {idempotency_key}")
        return result

    def ping(self) -> None:
        """Cheap SQLite liveness check for readiness probes."""
        with self._connect() as connection:
            connection.execute("SELECT 1").fetchone()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    @staticmethod
    def _record(row: sqlite3.Row) -> dict[str, Any]:
        record = dict(row)
        if record.get("result_json") is not None:
            record["result"] = json.loads(record["result_json"])
        if record.get("recovery_payload_json") is not None:
            try:
                record["recovery_payload"] = json.loads(record["recovery_payload_json"])
            except (TypeError, ValueError, json.JSONDecodeError):
                record["recovery_payload"] = None
        else:
            record["recovery_payload"] = None
        return record

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()


__all__ = ["ExecutionLedger", "LedgerClaim", "LedgerClaimLost", "canonical_arguments_hash"]
