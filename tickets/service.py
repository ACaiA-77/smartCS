"""Durable support-ticket operations backed by the application SQLite DB."""

from __future__ import annotations

import sqlite3
import hashlib
import json
import uuid
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from mcp.order_repository import OrderRepository


def _normalized_text(value: Any) -> str:
    return " ".join(str(value or "").split())


def canonical_ticket_payload_hash(
    payload: Mapping[str, Any] | str | None = None,
    title: Any = None,
    description: Any = None,
    priority: Any = None,
    category: Any = None,
    *,
    user_id: Any = None,
) -> str:
    """Return the stable hash for the caller-visible ticket payload."""
    source = dict(payload) if isinstance(payload, Mapping) else {}
    if not isinstance(payload, Mapping) and payload is not None:
        source["user_id"] = payload
    if user_id is not None:
        source["user_id"] = user_id
    if title is not None:
        source["title"] = title
    if description is not None:
        source["description"] = description
    if priority is not None:
        source["priority"] = priority
    if category is not None:
        source["category"] = category

    canonical = {
        "user_id": _normalized_text(source.get("user_id")),
        "category": _normalized_text(
            source.get("category", source.get("ticket_type", "general"))
        ).lower()
        or "general",
        "priority": _normalized_text(source.get("priority", "medium")).lower()
        or "medium",
        "title": _normalized_text(source.get("title")),
        "description": _normalized_text(source.get("description")),
    }
    return hashlib.sha256(
        json.dumps(
            canonical,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


class TicketService:
    """Create and query support tickets with client-request idempotency."""

    def __init__(self, repository: OrderRepository) -> None:
        self.repository = repository
        with self.repository.transaction() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS support_tickets (
                    ticket_id TEXT PRIMARY KEY,
                    client_request_id TEXT NOT NULL UNIQUE,
                    user_id TEXT NOT NULL,
                    category TEXT NOT NULL,
                    priority TEXT NOT NULL,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(support_tickets)").fetchall()
            }
            self._has_legacy_ticket_type = "ticket_type" in columns
            if "category" not in columns:
                connection.execute(
                    "ALTER TABLE support_tickets ADD COLUMN category TEXT NOT NULL DEFAULT 'general'"
                )
                if "ticket_type" in columns:
                    connection.execute(
                        "UPDATE support_tickets SET category = ticket_type"
                    )
            if "payload_hash" not in columns:
                connection.execute(
                    "ALTER TABLE support_tickets ADD COLUMN payload_hash TEXT NOT NULL DEFAULT ''"
                )
                rows = connection.execute(
                    "SELECT * FROM support_tickets WHERE payload_hash = ''"
                ).fetchall()
                for row in rows:
                    payload = {
                        "user_id": row["user_id"],
                        "category": row["category"] if "category" in row.keys() else row["ticket_type"],
                        "priority": row["priority"],
                        "title": row["title"],
                        "description": row["description"],
                    }
                    connection.execute(
                        "UPDATE support_tickets SET payload_hash = ? WHERE ticket_id = ?",
                        (self._payload_hash(payload), row["ticket_id"]),
                    )
            self._has_legacy_ticket_type = any(
                row["name"] == "ticket_type"
                for row in connection.execute("PRAGMA table_info(support_tickets)").fetchall()
            )

    def create_ticket(
        self,
        client_request_id: str,
        user_id: str,
        title: str,
        description: str,
        priority: str = "medium",
        ticket_type: str = "general",
        now: datetime | None = None,
        category: str | None = None,
        request_payload_hash: str | None = None,
    ) -> dict[str, Any]:
        if category is not None:
            ticket_type = category
        values = self._normalize(
            client_request_id=client_request_id,
            user_id=user_id,
            title=title,
            description=description,
            priority=priority,
            ticket_type=ticket_type,
        )
        payload_hash = canonical_ticket_payload_hash(values)
        if (
            request_payload_hash is not None
            and str(request_payload_hash).strip().lower() != payload_hash
        ):
            return self._failure("invalid_request_payload_hash", values)
        if not values["client_request_id"]:
            return self._failure("invalid_client_request_id", values)
        if not values["user_id"]:
            return self._failure("invalid_user_id", values)
        if not values["title"] or not values["description"]:
            return self._failure("invalid_ticket_content", values)

        timestamp = (now or datetime.now()).isoformat()
        with self.repository.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM support_tickets WHERE client_request_id = ?",
                (values["client_request_id"],),
            ).fetchone()
            if existing is not None:
                if self._same_request(existing, payload_hash):
                    result = self._as_dict(existing)
                    result.update({"success": True, "reason_code": "replayed", "replayed": True})
                    return result
                # A client-request conflict must not disclose the existing
                # ticket, including when the caller belongs to another user.
                return self._failure("client_request_conflict", values)

            ticket_id = f"TK-{uuid.uuid4().hex[:10].upper()}"
            columns = [
                "ticket_id",
                "client_request_id",
                "user_id",
                "category",
                "priority",
                "title",
                "description",
                "payload_hash",
                "status",
                "created_at",
                "updated_at",
            ]
            parameters: list[Any] = [
                ticket_id,
                values["client_request_id"],
                values["user_id"],
                values["ticket_type"],
                values["priority"],
                values["title"],
                values["description"],
                payload_hash,
                "created",
                timestamp,
                timestamp,
            ]
            if self._has_legacy_ticket_type:
                columns.insert(4, "ticket_type")
                parameters.insert(4, values["ticket_type"])
            placeholders = ", ".join("?" for _ in columns)
            connection.execute(
                f"INSERT INTO support_tickets ({', '.join(columns)}) VALUES ({placeholders})",
                parameters,
            )
            row = connection.execute(
                "SELECT * FROM support_tickets WHERE ticket_id = ?", (ticket_id,)
            ).fetchone()
            result = self._as_dict(row)
            result.update({"success": True, "reason_code": "created", "replayed": False})
            return result

    def query_ticket(self, ticket_id: str, user_id: str) -> dict[str, Any] | None:
        """Return a ticket only when it belongs to the requesting user."""
        normalized_ticket_id = str(ticket_id or "").strip()
        normalized_user_id = str(user_id or "").strip()
        if not normalized_ticket_id or not normalized_user_id:
            return None
        with self.repository.transaction() as connection:
            row = connection.execute(
                """
                SELECT * FROM support_tickets
                WHERE ticket_id = ? AND user_id = ?
                """,
                (normalized_ticket_id, normalized_user_id),
            ).fetchone()
        return self._as_dict(row) if row is not None else None

    get_ticket = query_ticket
    query = query_ticket
    create = create_ticket

    def find_by_client_request_id(
        self,
        client_request_id: str,
        user_id: str,
        request_payload_hash: str | None = None,
    ) -> dict[str, Any] | None:
        """Read the durable effect used by crash-window reconciliation."""
        normalized_request_id = str(client_request_id or "").strip()
        normalized_user_id = str(user_id or "").strip()
        if not normalized_request_id or not normalized_user_id:
            return None
        with self.repository.transaction() as connection:
            row = connection.execute(
                """
                SELECT * FROM support_tickets
                WHERE client_request_id = ? AND user_id = ?
                """,
                (normalized_request_id, normalized_user_id),
            ).fetchone()
        if row is None:
            return None
        if (
            request_payload_hash is not None
            and str(request_payload_hash).strip().lower() != row["payload_hash"]
        ):
            return {
                "success": False,
                "reason_code": "client_request_conflict",
                "client_request_id": normalized_request_id,
                "_payload_conflict": True,
            }
        result = self._as_dict(row)
        result.update({"success": True, "reason_code": "reconciled_existing_ticket", "replayed": True})
        return result

    # Compatibility alias for callers from the first recovery iteration.
    find_existing_ticket_effect = find_by_client_request_id

    @staticmethod
    def _normalize(**values: str) -> dict[str, str]:
        category = _normalized_text(values.get("ticket_type", "general")).lower() or "general"
        return {
            "client_request_id": _normalized_text(values.get("client_request_id")),
            "user_id": _normalized_text(values.get("user_id")),
            "title": _normalized_text(values.get("title")),
            "description": _normalized_text(values.get("description")),
            "priority": _normalized_text(values.get("priority", "medium")).lower() or "medium",
            "category": category,
            "ticket_type": category,
        }

    @staticmethod
    def _same_request(row: sqlite3.Row, payload_hash: str) -> bool:
        return row["payload_hash"] == payload_hash

    @staticmethod
    def _payload_hash(values: dict[str, str]) -> str:
        return canonical_ticket_payload_hash(values)

    @classmethod
    def _failure(
        cls,
        reason_code: str,
        values: dict[str, str],
        existing: sqlite3.Row | None = None,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {
            "success": False,
            "reason_code": reason_code,
            "client_request_id": values["client_request_id"],
        }
        if reason_code != "client_request_conflict":
            result["user_id"] = values["user_id"]
        if existing is not None:
            result.update({"ticket_id": existing["ticket_id"], "status": existing["status"]})
        return result

    @staticmethod
    def _as_dict(row: sqlite3.Row | None) -> dict[str, Any]:
        if row is None:
            raise RuntimeError("ticket row is missing after insert")
        result = dict(row)
        result["ticket_type"] = result["category"]
        result["type"] = result["category"]
        result["summary"] = result["title"]
        result["details"] = result["description"]
        return result


__all__ = ["TicketService", "canonical_ticket_payload_hash"]
