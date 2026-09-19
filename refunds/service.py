"""Refund eligibility and creation against the SQLite business sandbox."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from mcp.order_repository import OrderRepository


_ACTIVE_REFUND_STATUSES = ("pending", "requested", "reviewing", "approved", "processing")
_RECOVERABLE_REFUND_STATUSES = _ACTIVE_REFUND_STATUSES + ("completed", "refunded")
_PAID_ORDER_STATUSES = {"paid", "processing", "shipped", "in_transit", "delivered"}
_REFUND_ID_PREFIX = "REF-SIM-"


@dataclass(frozen=True)
class RefundEligibility:
    order_id: str
    user_id: str
    eligible: bool
    reason_code: str
    refund_mode: str | None = None
    amount: float | None = None
    payment_id: str | None = None

    @property
    def mode(self) -> str | None:
        return self.refund_mode

    @property
    def refund_type(self) -> str | None:
        return self.refund_mode

    @property
    def is_eligible(self) -> bool:
        return self.eligible

    def as_dict(self) -> dict[str, Any]:
        return {
            "order_id": self.order_id,
            "user_id": self.user_id,
            "eligible": self.eligible,
            "reason_code": self.reason_code,
            "refund_mode": self.refund_mode,
            "amount": self.amount,
            "payment_id": self.payment_id,
        }

    def __getitem__(self, key: str) -> Any:
        return self.as_dict()[key]


@dataclass(frozen=True)
class RefundResult:
    order_id: str
    user_id: str
    success: bool
    reason_code: str
    refund_id: str | None = None
    payment_id: str | None = None
    amount: float | None = None
    status: str | None = None
    refund_mode: str | None = None
    requested_at: str | None = None
    existing_refund_id: str | None = None

    @property
    def ok(self) -> bool:
        return self.success

    @property
    def created(self) -> bool:
        return self.success

    @property
    def mode(self) -> str | None:
        return self.refund_mode

    @property
    def refund_type(self) -> str | None:
        return self.refund_mode

    def as_dict(self) -> dict[str, Any]:
        return {
            "order_id": self.order_id,
            "user_id": self.user_id,
            "success": self.success,
            "reason_code": self.reason_code,
            "refund_id": self.refund_id,
            "payment_id": self.payment_id,
            "amount": self.amount,
            "status": self.status,
            "refund_mode": self.refund_mode,
            "requested_at": self.requested_at,
            "existing_refund_id": self.existing_refund_id,
        }

    def __getitem__(self, key: str) -> Any:
        return self.as_dict()[key]


class RefundService:
    """Pure eligibility checks plus one-transaction refund creation."""

    def __init__(self, repository: OrderRepository):
        self.repository = repository

    def evaluate(self, order_id: str, user_id: str) -> RefundEligibility:
        with self.repository.transaction() as connection:
            return self._evaluate_connection(connection, order_id, user_id)

    def create_refund(
        self,
        order_id: str,
        user_id: str,
        reason: str,
        now: datetime | None = None,
    ) -> RefundResult:
        timestamp = _timestamp(datetime.now() if now is None else now)
        with self.repository.transaction() as connection:
            order = self._order(connection, order_id)
            if order is None:
                return RefundResult(order_id, user_id, False, "not_found")
            if order["user_id"] != user_id:
                return RefundResult(order_id, user_id, False, "order_not_owned")

            payment = self._paid_payment(connection, order_id)
            active = self._active_refund(connection, order_id)
            if active is not None:
                amount = payment["amount"] if payment is not None else None
                payment_id = payment["payment_id"] if payment is not None else active["payment_id"]
                return RefundResult(
                    order_id,
                    user_id,
                    False,
                    "refund_already_pending",
                    refund_id=active["refund_id"],
                    payment_id=payment_id,
                    amount=amount,
                    status=active["status"],
                    existing_refund_id=active["refund_id"],
                )

            eligibility = self._evaluate_connection(connection, order_id, user_id)
            if not eligibility.eligible:
                return RefundResult(
                    order_id,
                    user_id,
                    False,
                    eligibility.reason_code,
                    payment_id=eligibility.payment_id,
                    amount=eligibility.amount,
                    refund_mode=eligibility.refund_mode,
                )
            if payment is None:
                # The detail payment is authoritative; legacy paid flags are not enough.
                return RefundResult(order_id, user_id, False, "not_paid")

            refund_id = self._next_refund_id(connection)
            connection.execute(
                """
                INSERT INTO refunds (
                    refund_id, order_id, payment_id, amount, reason, status,
                    requested_at, completed_at
                ) VALUES (?, ?, ?, ?, ?, 'pending', ?, NULL)
                """,
                (
                    refund_id,
                    order_id,
                    payment["payment_id"],
                    payment["amount"],
                    reason,
                    timestamp,
                ),
            )
            self._update_order_legacy(connection, order_id, timestamp)
            return RefundResult(
                order_id,
                user_id,
                True,
                "created",
                refund_id=refund_id,
                payment_id=payment["payment_id"],
                amount=payment["amount"],
                status="pending",
                refund_mode=eligibility.refund_mode,
                requested_at=timestamp,
            )

    def find_existing_refund_effect(
        self, order_id: str, user_id: str
    ) -> dict[str, Any] | None:
        """Return an authoritative refund effect without creating or changing anything."""
        marks = ", ".join("?" for _ in _RECOVERABLE_REFUND_STATUSES)
        with self.repository.transaction() as connection:
            order = self._order(connection, order_id)
            if order is None or order["user_id"] != user_id:
                return None
            row = connection.execute(
                f"""
                SELECT r.refund_id, r.order_id, r.payment_id, r.amount, r.status,
                       r.requested_at, p.payment_id AS joined_payment_id
                FROM refunds r
                LEFT JOIN payments p ON p.payment_id = r.payment_id
                WHERE r.order_id = ? AND r.status IN ({marks})
                ORDER BY r.requested_at, r.refund_id
                LIMIT 1
                """,
                (order_id, *_RECOVERABLE_REFUND_STATUSES),
            ).fetchone()
            if row is None:
                return None
            return {
                "success": True,
                "order_id": row["order_id"],
                "user_id": user_id,
                "refund_id": row["refund_id"],
                "payment_id": row["payment_id"] or row["joined_payment_id"],
                "amount": row["amount"],
                "status": row["status"],
                "requested_at": row["requested_at"],
                "refund_mode": None,
                "reason_code": "reconciled_existing_refund",
            }

    @staticmethod
    def _order(connection: sqlite3.Connection, order_id: str) -> sqlite3.Row | None:
        return connection.execute("SELECT * FROM orders WHERE order_id = ?", (order_id,)).fetchone()

    @staticmethod
    def _paid_payment(connection: sqlite3.Connection, order_id: str) -> sqlite3.Row | None:
        return connection.execute(
            """
            SELECT * FROM payments
            WHERE order_id = ? AND status = 'paid'
            ORDER BY created_at DESC, payment_id DESC
            LIMIT 1
            """,
            (order_id,),
        ).fetchone()

    @staticmethod
    def _active_refund(connection: sqlite3.Connection, order_id: str) -> sqlite3.Row | None:
        marks = ", ".join("?" for _ in _ACTIVE_REFUND_STATUSES)
        return connection.execute(
            f"""
            SELECT * FROM refunds
            WHERE order_id = ? AND status IN ({marks})
            ORDER BY requested_at, refund_id
            LIMIT 1
            """,
            (order_id, *_ACTIVE_REFUND_STATUSES),
        ).fetchone()

    @classmethod
    def _evaluate_connection(
        cls, connection: sqlite3.Connection, order_id: str, user_id: str
    ) -> RefundEligibility:
        order = cls._order(connection, order_id)
        if order is None:
            return RefundEligibility(order_id, user_id, False, "not_found")
        if order["user_id"] != user_id:
            return RefundEligibility(order_id, user_id, False, "order_not_owned")

        payment = cls._paid_payment(connection, order_id)
        payment_id = payment["payment_id"] if payment is not None else None
        amount = payment["amount"] if payment is not None else None
        status = order["status"]
        if status == "refunded":
            return RefundEligibility(order_id, user_id, False, "already_refunded", amount=amount, payment_id=payment_id)
        if status == "cancelled":
            return RefundEligibility(order_id, user_id, False, "order_cancelled", amount=amount, payment_id=payment_id)
        if status == "pending_payment":
            return RefundEligibility(order_id, user_id, False, "not_paid")
        if connection.execute(
            "SELECT 1 FROM refunds WHERE order_id = ? AND status IN ('completed', 'refunded') LIMIT 1",
            (order_id,),
        ).fetchone():
            return RefundEligibility(order_id, user_id, False, "already_refunded", amount=amount, payment_id=payment_id)
        if status == "refund_pending" or cls._active_refund(connection, order_id) is not None:
            return RefundEligibility(
                order_id,
                user_id,
                False,
                "refund_already_pending",
                amount=amount,
                payment_id=payment_id,
            )
        if status in _PAID_ORDER_STATUSES:
            if payment is None:
                return RefundEligibility(order_id, user_id, False, "not_paid")
            mode = "refund_only" if status in {"paid", "processing"} else "return_and_refund"
            return RefundEligibility(
                order_id,
                user_id,
                True,
                "eligible",
                refund_mode=mode,
                amount=amount,
                payment_id=payment_id,
            )
        return RefundEligibility(order_id, user_id, False, "unsupported_status", amount=amount, payment_id=payment_id)

    @staticmethod
    def _next_refund_id(connection: sqlite3.Connection) -> str:
        used = {
            row["refund_id"]
            for row in connection.execute(
                "SELECT refund_id FROM refunds WHERE refund_id LIKE ?", (f"{_REFUND_ID_PREFIX}%",)
            )
        }
        number = 1
        while f"{_REFUND_ID_PREFIX}{number:06d}" in used:
            number += 1
        return f"{_REFUND_ID_PREFIX}{number:06d}"

    @staticmethod
    def _update_order_legacy(connection: sqlite3.Connection, order_id: str, timestamp: str) -> None:
        cursor = connection.execute(
            """
            UPDATE orders
            SET status = 'refund_pending', status_label = '退款审核中',
                payment_status = 'paid', payment_status_label = '已支付',
                after_sale_status = 'refund_pending', after_sale_status_label = '退款审核中',
                updated_at = ?
            WHERE order_id = ?
            """,
            (timestamp, order_id),
        )
        if cursor.rowcount != 1:
            raise RuntimeError(f"order disappeared during refund creation: {order_id}")


def _timestamp(value: datetime) -> str:
    if not isinstance(value, datetime):
        raise TypeError("now must be a datetime or None")
    return value.isoformat(timespec="seconds")
