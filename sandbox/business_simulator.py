"""Deterministic, transaction-backed e-commerce business simulator."""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from mcp.order_repository import OrderRepository


_ORDER_PREFIX = "ORD-SIM-"
_COURIERS = (("顺丰速运", "SF"), ("京东物流", "JD"), ("中通快递", "ZT"), ("圆通速递", "YT"))
_STATE_FIELDS = {
    "pending_payment": ("待付款", "unpaid", "未支付", "无", "无"),
    "paid": ("待发货", "paid", "已支付", "无", "无"),
    "processing": ("备货中", "paid", "已支付", "无", "无"),
    "shipped": ("已发货", "paid", "已支付", "无", "无"),
    "in_transit": ("运输中", "paid", "已支付", "无", "无"),
    "delivered": ("已签收", "paid", "已支付", "无", "无"),
    "refund_pending": ("退款审核中", "paid", "已支付", "refund_pending", "退款审核中"),
    "refunded": ("已退款", "refunded", "已退款", "refunded", "退款完成"),
    "cancelled": ("已取消", "cancelled", "已取消", "无", "无"),
}
_TRANSITIONS = {
    "pending_payment": ("paid", "cancelled"),
    "paid": ("processing",),
    "processing": ("shipped",),
    "shipped": ("in_transit",),
    "in_transit": ("delivered",),
    "refund_pending": ("refunded",),
}


@dataclass(frozen=True)
class Transition:
    order_id: str
    from_status: str
    to_status: str
    reason: str

    @property
    def from_state(self) -> str:
        return self.from_status

    @property
    def to_state(self) -> str:
        return self.to_status

    def as_dict(self) -> dict[str, str]:
        return {
            "order_id": self.order_id,
            "from_status": self.from_status,
            "to_status": self.to_status,
            "reason": self.reason,
        }

    def __getitem__(self, key: str) -> str:
        return self.as_dict()[key]


@dataclass(frozen=True)
class TickResult:
    now: datetime
    transitions: tuple[Transition, ...] = ()
    created_order_ids: tuple[str, ...] = ()

    @property
    def transition_count(self) -> int:
        return len(self.transitions)

    @property
    def created_count(self) -> int:
        return len(self.created_order_ids)

    @property
    def created_orders(self) -> tuple[str, ...]:
        return self.created_order_ids

    def as_dict(self) -> dict[str, Any]:
        return {
            "now": self.now.isoformat(timespec="seconds"),
            "created_order_ids": list(self.created_order_ids),
            "transitions": [transition.as_dict() for transition in self.transitions],
            "transition_count": self.transition_count,
        }


class BusinessSimulator:
    """Advance synthetic orders one deterministic state at a time."""

    def __init__(self, repository: OrderRepository):
        self.repository = repository

    def tick(
        self,
        now: datetime | None = None,
        max_transitions: int = 100,
        create_orders: int = 0,
    ) -> TickResult:
        tick_now = datetime.now().replace(microsecond=0) if now is None else now
        if not isinstance(tick_now, datetime):
            raise TypeError("now must be a datetime or None")
        if max_transitions < 0:
            raise ValueError("max_transitions must be non-negative")
        if create_orders < 0:
            raise ValueError("create_orders must be non-negative")
        timestamp = tick_now.isoformat(timespec="seconds")

        with self.repository.transaction() as connection:
            created = self._create_orders(connection, timestamp, create_orders)
            excluded = set(created)
            transitions: list[Transition] = []
            if max_transitions:
                rows = connection.execute(
                    """
                    SELECT * FROM orders
                    WHERE status IN ('pending_payment', 'paid', 'processing',
                                     'shipped', 'in_transit', 'refund_pending')
                    ORDER BY updated_at, order_id
                    """
                ).fetchall()
                for row in rows:
                    if row["order_id"] in excluded:
                        continue
                    transition = self._transition(connection, row, timestamp)
                    transitions.append(transition)
                    if len(transitions) >= max_transitions:
                        break

        return TickResult(tick_now, tuple(transitions), tuple(created))

    @staticmethod
    def _create_orders(connection: sqlite3.Connection, timestamp: str, count: int) -> list[str]:
        if not count:
            return []
        users = [row["user_id"] for row in connection.execute("SELECT user_id FROM users ORDER BY user_id")]
        if not users:
            raise RuntimeError("cannot create simulator orders without users")
        catalog = connection.execute(
            """
            SELECT product_name, sku_name, unit_price
            FROM order_items ORDER BY item_id LIMIT 5
            """
        ).fetchall()
        if not catalog:
            raise RuntimeError("cannot create simulator orders without catalog items")

        used_ids = {
            row["order_id"]
            for row in connection.execute(
                "SELECT order_id FROM orders WHERE order_id LIKE ?", (f"{_ORDER_PREFIX}%",)
            )
        }
        next_number = 1
        for order_id in used_ids:
            suffix = order_id.removeprefix(_ORDER_PREFIX)
            if suffix.isdigit():
                next_number = max(next_number, int(suffix) + 1)

        created: list[str] = []
        for offset in range(count):
            while True:
                order_id = f"{_ORDER_PREFIX}{next_number:06d}"
                next_number += 1
                if order_id not in used_ids:
                    break
            used_ids.add(order_id)
            product_name, sku_name, unit_price = catalog[offset % len(catalog)]
            connection.execute(
                """
                INSERT INTO orders (
                    order_id, user_id, status, status_label, payment_status,
                    payment_status_label, original_amount, discount_amount, pay_amount,
                    recipient_name_masked, recipient_phone_masked, city, courier_company,
                    tracking_number, shipped_at, delivered_at, after_sale_status,
                    after_sale_status_label, created_at, updated_at
                ) VALUES (?, ?, 'pending_payment', '待付款', 'unpaid', '未支付', ?, 0, ?,
                          ?, ?, ?, NULL, NULL, NULL, NULL, '无', '无', ?, ?)
                """,
                (
                    order_id,
                    users[offset % len(users)],
                    unit_price,
                    unit_price,
                    "张*" if offset % 2 == 0 else "李*",
                    "138****1201" if offset % 2 == 0 else "139****4820",
                    "上海市浦东新区" if offset % 2 == 0 else "北京市朝阳区",
                    timestamp,
                    timestamp,
                ),
            )
            connection.execute(
                """
                INSERT INTO order_items
                    (order_id, product_name, sku_name, unit_price, quantity, item_total)
                VALUES (?, ?, ?, ?, 1, ?)
                """,
                (order_id, product_name, sku_name, unit_price, unit_price),
            )
            created.append(order_id)
        return created

    def _transition(self, connection: sqlite3.Connection, row: sqlite3.Row, timestamp: str) -> Transition:
        order_id = row["order_id"]
        from_status = row["status"]
        if from_status not in _TRANSITIONS:
            raise RuntimeError(f"unsupported simulator state: {from_status}")
        if from_status == "pending_payment":
            to_status = "cancelled" if self._should_cancel(order_id) else "paid"
            if to_status == "paid":
                self._mark_paid(connection, row, timestamp)
                reason = "deterministic payment"
            else:
                self._ensure_no_successful_payment(connection, order_id)
                reason = "deterministic cancellation"
        elif from_status == "paid":
            self._require_paid_payment(connection, order_id)
            to_status, reason = "processing", "payment settled"
        elif from_status == "processing":
            self._require_paid_payment(connection, order_id)
            carrier, tracking_number = self._create_shipment(connection, order_id, timestamp)
            connection.execute(
                """
                UPDATE orders
                SET courier_company = ?, tracking_number = ?, shipped_at = ?
                WHERE order_id = ?
                """,
                (carrier, tracking_number, timestamp, order_id),
            )
            to_status, reason = "shipped", "shipment created"
        elif from_status == "shipped":
            self._update_shipment(connection, order_id, "in_transit", timestamp)
            to_status, reason = "in_transit", "carrier accepted shipment"
        elif from_status == "in_transit":
            self._update_shipment(connection, order_id, "delivered", timestamp)
            connection.execute(
                "UPDATE orders SET delivered_at = ? WHERE order_id = ?", (timestamp, order_id)
            )
            to_status, reason = "delivered", "delivery completed"
        else:
            self._complete_refund(connection, row, timestamp)
            to_status, reason = "refunded", "refund completed"

        self._set_legacy_state(connection, order_id, to_status, timestamp)
        return Transition(order_id, from_status, to_status, reason)

    @staticmethod
    def _should_cancel(order_id: str) -> bool:
        return hashlib.sha256(order_id.encode("utf-8")).digest()[0] % 4 == 0

    @staticmethod
    def _set_legacy_state(connection: sqlite3.Connection, order_id: str, status: str, timestamp: str) -> None:
        fields = _STATE_FIELDS[status]
        connection.execute(
            """
            UPDATE orders
            SET status = ?, status_label = ?, payment_status = ?, payment_status_label = ?,
                after_sale_status = ?, after_sale_status_label = ?, updated_at = ?
            WHERE order_id = ?
            """,
            (status, *fields, timestamp, order_id),
        )

    @staticmethod
    def _mark_paid(connection: sqlite3.Connection, row: sqlite3.Row, timestamp: str) -> None:
        existing = connection.execute(
            "SELECT payment_id FROM payments WHERE order_id = ? ORDER BY payment_id LIMIT 1",
            (row["order_id"],),
        ).fetchone()
        payment_id = existing["payment_id"] if existing else f"PAY-{row['order_id']}"
        if existing:
            connection.execute(
                "UPDATE payments SET amount = ?, status = 'paid', paid_at = ? WHERE payment_id = ?",
                (row["pay_amount"], timestamp, payment_id),
            )
        else:
            connection.execute(
                """
                INSERT INTO payments (payment_id, order_id, amount, status, paid_at, created_at)
                VALUES (?, ?, ?, 'paid', ?, ?)
                """,
                (payment_id, row["order_id"], row["pay_amount"], timestamp, timestamp),
            )

    @staticmethod
    def _ensure_no_successful_payment(connection: sqlite3.Connection, order_id: str) -> None:
        if connection.execute(
            "SELECT 1 FROM payments WHERE order_id = ? AND status = 'paid' LIMIT 1", (order_id,)
        ).fetchone():
            raise RuntimeError(f"pending order {order_id} already has a successful payment")

    @staticmethod
    def _require_paid_payment(connection: sqlite3.Connection, order_id: str) -> sqlite3.Row:
        payment = connection.execute(
            "SELECT * FROM payments WHERE order_id = ? AND status = 'paid' ORDER BY created_at DESC LIMIT 1",
            (order_id,),
        ).fetchone()
        if payment is None:
            raise RuntimeError(f"order {order_id} has no paid payment")
        return payment

    @staticmethod
    def _create_shipment(
        connection: sqlite3.Connection, order_id: str, timestamp: str
    ) -> tuple[str, str]:
        carrier, tracking_number = _shipment_identity(order_id)
        existing = connection.execute(
            "SELECT shipment_id FROM shipments WHERE order_id = ? ORDER BY shipment_id LIMIT 1", (order_id,)
        ).fetchone()
        if existing:
            connection.execute(
                """
                UPDATE shipments
                SET carrier = ?, tracking_number = ?, status = 'shipped', shipped_at = ?,
                    delivered_at = NULL, updated_at = ?
                WHERE shipment_id = ?
                """,
                (carrier, tracking_number, timestamp, timestamp, existing["shipment_id"]),
            )
        else:
            connection.execute(
                """
                INSERT INTO shipments (
                    shipment_id, order_id, carrier, tracking_number, status,
                    shipped_at, delivered_at, updated_at
                ) VALUES (?, ?, ?, ?, 'shipped', ?, NULL, ?)
                """,
                (f"SHP-{order_id}", order_id, carrier, tracking_number, timestamp, timestamp),
            )
        return carrier, tracking_number

    @staticmethod
    def _update_shipment(connection: sqlite3.Connection, order_id: str, status: str, timestamp: str) -> None:
        shipment = connection.execute(
            "SELECT shipment_id FROM shipments WHERE order_id = ? ORDER BY shipment_id LIMIT 1", (order_id,)
        ).fetchone()
        if shipment is None:
            raise RuntimeError(f"order {order_id} has no shipment")
        if status == "delivered":
            connection.execute(
                "UPDATE shipments SET status = ?, delivered_at = ?, updated_at = ? WHERE shipment_id = ?",
                (status, timestamp, timestamp, shipment["shipment_id"]),
            )
        else:
            connection.execute(
                "UPDATE shipments SET status = ?, updated_at = ? WHERE shipment_id = ?",
                (status, timestamp, shipment["shipment_id"]),
            )

    @staticmethod
    def _complete_refund(connection: sqlite3.Connection, row: sqlite3.Row, timestamp: str) -> None:
        payment = BusinessSimulator._require_paid_payment(connection, row["order_id"])
        refund = connection.execute(
            """
            SELECT refund_id FROM refunds
            WHERE order_id = ? AND payment_id = ?
              AND status IN ('pending', 'requested', 'reviewing', 'approved', 'processing')
            ORDER BY requested_at, refund_id LIMIT 1
            """,
            (row["order_id"], payment["payment_id"]),
        ).fetchone()
        if refund is None:
            raise RuntimeError(f"order {row['order_id']} has no pending refund")
        connection.execute(
            "UPDATE refunds SET status = 'completed', completed_at = ? WHERE refund_id = ?",
            (timestamp, refund["refund_id"]),
        )


def _shipment_identity(order_id: str) -> tuple[str, str]:
    digest = hashlib.sha256(order_id.encode("utf-8")).hexdigest()
    carrier, prefix = _COURIERS[int(digest[:8], 16) % len(_COURIERS)]
    return carrier, f"{prefix}{digest[8:24].upper()}"
