"""SQLite-backed domestic e-commerce demo order repository."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


class OrderRepository:
    """Reads a deterministic local dataset of 100 domestic e-commerce orders."""

    DEMO_DATA_SOURCE = "SQLite 本地国内电商演示数据"

    def __init__(self, db_path: str = "./data/orders.db"):
        self.db_path = Path(db_path)
        self.initialize()

    def initialize(self) -> int:
        """Create the schema and seed missing demo orders. Returns the order count."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            self._create_schema(connection)
            self._seed_demo_users(connection)
            self._seed_demo_orders(connection)
            self._seed_order_details(connection)
            return int(connection.execute("SELECT COUNT(*) FROM orders").fetchone()[0])

    def count_orders(self) -> int:
        with self._connect() as connection:
            return int(connection.execute("SELECT COUNT(*) FROM orders").fetchone()[0])

    def get_order(self, order_id: str) -> dict[str, Any] | None:
        """Return one order and its line items, or None when the order does not exist."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM orders WHERE order_id = ?",
                (order_id,),
            ).fetchone()
            if row is None:
                return None

            items = connection.execute(
                """
                SELECT product_name, sku_name, unit_price, quantity, item_total
                FROM order_items
                WHERE order_id = ?
                ORDER BY item_id
                """,
                (order_id,),
            ).fetchall()
            payment = connection.execute(
                "SELECT * FROM payments WHERE order_id = ? ORDER BY created_at DESC, payment_id DESC LIMIT 1",
                (order_id,),
            ).fetchone()
            shipment = connection.execute(
                "SELECT * FROM shipments WHERE order_id = ? ORDER BY updated_at DESC, shipment_id DESC LIMIT 1",
                (order_id,),
            ).fetchone()
            refunds = connection.execute(
                "SELECT * FROM refunds WHERE order_id = ? ORDER BY requested_at, refund_id",
                (order_id,),
            ).fetchall()

        result = dict(row)
        result["items"] = [dict(item) for item in items]
        result["product"] = self._product_summary(result["items"])
        result["payment"] = dict(payment) if payment is not None else None
        result["shipment"] = dict(shipment) if shipment is not None else None
        result["refunds"] = [dict(refund) for refund in refunds]
        return result

    def get_order_for_user(self, order_id: str, user_id: str) -> dict[str, Any] | None:
        """Treat an order belonging to another user exactly like a missing order."""
        order = self.get_order(order_id)
        return order if order is not None and order["user_id"] == user_id else None

    def list_orders(self, limit: int = 6) -> list[dict[str, Any]]:
        """List recent demo orders for the web workbench quick-action panel."""
        safe_limit = max(1, min(limit, 20))
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT order_id, status, status_label, pay_amount, created_at
                FROM orders
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (safe_limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_orders_for_user(self, user_id: str, limit: int = 6) -> list[dict[str, Any]]:
        """List only the authenticated customer's recent sandbox orders."""
        safe_limit = max(1, min(limit, 20))
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT order_id, status, status_label, pay_amount, created_at
                FROM orders
                WHERE user_id = ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (user_id, safe_limit),
            ).fetchall()
        return [dict(row) for row in rows]

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.db_path)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("BEGIN")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Expose one repository-owned transaction for business mutations."""
        with self._connect() as connection:
            yield connection

    @staticmethod
    def _create_schema(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id TEXT PRIMARY KEY,
                display_name TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS orders (
                order_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                status TEXT NOT NULL,
                status_label TEXT NOT NULL,
                payment_status TEXT NOT NULL,
                payment_status_label TEXT NOT NULL,
                original_amount REAL NOT NULL,
                discount_amount REAL NOT NULL,
                pay_amount REAL NOT NULL,
                recipient_name_masked TEXT NOT NULL,
                recipient_phone_masked TEXT NOT NULL,
                city TEXT NOT NULL,
                courier_company TEXT,
                tracking_number TEXT,
                shipped_at TEXT,
                delivered_at TEXT,
                after_sale_status TEXT NOT NULL,
                after_sale_status_label TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(user_id)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS order_items (
                item_id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id TEXT NOT NULL,
                product_name TEXT NOT NULL,
                sku_name TEXT NOT NULL,
                unit_price REAL NOT NULL,
                quantity INTEGER NOT NULL,
                item_total REAL NOT NULL,
                FOREIGN KEY(order_id) REFERENCES orders(order_id)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS payments (
                payment_id TEXT PRIMARY KEY,
                order_id TEXT NOT NULL,
                amount REAL NOT NULL CHECK(amount >= 0),
                status TEXT NOT NULL,
                paid_at TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(order_id) REFERENCES orders(order_id) ON DELETE CASCADE
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS shipments (
                shipment_id TEXT PRIMARY KEY,
                order_id TEXT NOT NULL,
                carrier TEXT NOT NULL,
                tracking_number TEXT NOT NULL,
                status TEXT NOT NULL,
                shipped_at TEXT,
                delivered_at TEXT,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(order_id) REFERENCES orders(order_id) ON DELETE CASCADE
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS refunds (
                refund_id TEXT PRIMARY KEY,
                order_id TEXT NOT NULL,
                payment_id TEXT NOT NULL,
                amount REAL NOT NULL CHECK(amount >= 0),
                reason TEXT NOT NULL,
                status TEXT NOT NULL,
                requested_at TEXT NOT NULL,
                completed_at TEXT,
                FOREIGN KEY(order_id) REFERENCES orders(order_id) ON DELETE CASCADE,
                FOREIGN KEY(payment_id) REFERENCES payments(payment_id),
                FOREIGN KEY(payment_id, order_id) REFERENCES payments(payment_id, order_id)
            )
            """
        )

        # The composite FK above needs a matching unique key on the parent.
        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_payments_payment_order ON payments(payment_id, order_id)"
        )

    @staticmethod
    def _seed_demo_users(connection: sqlite3.Connection) -> None:
        """Add users needed by demo orders without changing existing user rows."""
        base_time = datetime(2026, 7, 1, 9, 0, 0)
        user_ids = {
            f"user_{index:03d}" for index in range(1, 21)
        }
        user_ids.update(
            row[0]
            for row in connection.execute(
                "SELECT DISTINCT user_id FROM orders WHERE user_id IS NOT NULL"
            ).fetchall()
        )
        for index, user_id in enumerate(sorted(user_ids)):
            connection.execute(
                """
                INSERT INTO users (user_id, display_name, created_at)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO NOTHING
                """,
                (
                    user_id,
                    f"演示用户 {user_id.removeprefix('user_')}",
                    (base_time + timedelta(minutes=17 * index)).isoformat(timespec="seconds"),
                ),
            )

    @staticmethod
    def _seed_demo_orders(connection: sqlite3.Connection) -> None:
        catalog = [
            ("iPhone 16", "256GB 黑色", 6999.00),
            ("AirPods Pro", "第二代 USB-C", 1899.00),
            ("MacBook Air", "M4 16GB+512GB", 9999.00),
            ("小米 15", "12GB+256GB 白色", 4499.00),
            ("华为 Mate 70", "12GB+512GB 墨黑", 6499.00),
            ("戴森吹风机", "HD15 紫红色", 3299.00),
            ("索尼降噪耳机", "WH-1000XM6 黑色", 2899.00),
            ("Switch 2", "标准版", 3499.00),
            ("机械键盘", "青轴 87 键", 459.00),
            ("人体工学椅", "黑色 网布款", 1299.00),
        ]
        cities = ["上海市浦东新区", "北京市朝阳区", "广州市天河区", "深圳市南山区", "杭州市西湖区"]
        recipients = [("张*", "138****1201"), ("李*", "139****4820"), ("王*", "136****0935"), ("陈*", "137****6128")]
        states = [
            ("pending_payment", "待付款", "unpaid", "未支付", "无", "无"),
            ("paid", "待发货", "paid", "已支付", "无", "无"),
            ("processing", "备货中", "paid", "已支付", "无", "无"),
            ("shipped", "已发货", "paid", "已支付", "无", "无"),
            ("in_transit", "运输中", "paid", "已支付", "无", "无"),
            ("delivered", "已签收", "paid", "已支付", "无", "无"),
            ("refund_pending", "退款审核中", "paid", "已支付", "refund_pending", "退款审核中"),
            ("refunded", "已退款", "refunded", "已退款", "refunded", "退款完成"),
            ("cancelled", "已取消", "cancelled", "已取消", "无", "无"),
        ]
        courier_specs = [
            ("顺丰速运", "SF"),
            ("京东物流", "JD"),
            ("中通快递", "ZT"),
            ("圆通速递", "YT"),
        ]
        base_time = datetime(2026, 7, 26, 9, 0, 0)

        for index in range(1, 101):
            order_id = f"ORD-20260801-{index:04d}"
            exists = connection.execute(
                "SELECT 1 FROM orders WHERE order_id = ?",
                (order_id,),
            ).fetchone()
            if exists:
                continue

            status, status_label, payment_status, payment_label, after_sale, after_sale_label = states[(index - 1) % len(states)]
            created_at = base_time + timedelta(minutes=85 * index)
            selected_products = [catalog[(index - 1) % len(catalog)]]
            if index % 3 == 0:
                selected_products.append(catalog[index % len(catalog)])

            item_rows: list[tuple[str, str, float, int, float]] = []
            original_amount = 0.0
            for item_index, (product_name, sku_name, unit_price) in enumerate(selected_products, start=1):
                quantity = 1 + ((index + item_index) % 2)
                item_total = unit_price * quantity
                original_amount += item_total
                item_rows.append((product_name, sku_name, unit_price, quantity, item_total))

            discount_amount = round(original_amount * (0.03 if index % 4 else 0.08), 2)
            pay_amount = round(original_amount - discount_amount, 2)
            courier_spec = courier_specs[index % len(courier_specs)] if status in {"shipped", "in_transit", "delivered"} else None
            courier = courier_spec[0] if courier_spec else None
            tracking_number = f"{courier_spec[1]}{202608010000 + index}" if courier_spec else None
            shipped_at = (created_at + timedelta(hours=18)).isoformat(timespec="seconds") if courier else None
            delivered_at = (created_at + timedelta(days=3)).isoformat(timespec="seconds") if status == "delivered" else None
            recipient_name, recipient_phone = recipients[index % len(recipients)]

            connection.execute(
                """
                INSERT INTO orders (
                    order_id, user_id, status, status_label, payment_status,
                    payment_status_label, original_amount, discount_amount, pay_amount,
                    recipient_name_masked, recipient_phone_masked, city, courier_company,
                    tracking_number, shipped_at, delivered_at, after_sale_status,
                    after_sale_status_label, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    order_id,
                    f"user_{((index - 1) % 20) + 1:03d}",
                    status,
                    status_label,
                    payment_status,
                    payment_label,
                    original_amount,
                    discount_amount,
                    pay_amount,
                    recipient_name,
                    recipient_phone,
                    cities[index % len(cities)],
                    courier,
                    tracking_number,
                    shipped_at,
                    delivered_at,
                    after_sale,
                    after_sale_label,
                    created_at.isoformat(timespec="seconds"),
                    (created_at + timedelta(minutes=20)).isoformat(timespec="seconds"),
                ),
            )
            connection.executemany(
                """
                INSERT INTO order_items (
                    order_id, product_name, sku_name, unit_price, quantity, item_total
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    (order_id, product_name, sku_name, unit_price, quantity, item_total)
                    for product_name, sku_name, unit_price, quantity, item_total in item_rows
                ],
            )

    @staticmethod
    def _seed_order_details(connection: sqlite3.Connection) -> None:
        """Fill missing detail rows from legacy order columns, preserving edits."""
        rows = connection.execute("SELECT * FROM orders ORDER BY order_id").fetchall()
        for row in rows:
            order_id = row["order_id"]
            status = row["status"]
            paid = status in {
                "paid",
                "processing",
                "shipped",
                "in_transit",
                "delivered",
                "refund_pending",
                "refunded",
            }
            payment_id = f"PAY-{order_id.removeprefix('ORD-')}"
            any_payment = connection.execute(
                "SELECT payment_id FROM payments WHERE order_id = ? "
                "ORDER BY created_at DESC, payment_id DESC LIMIT 1",
                (order_id,),
            ).fetchone()
            existing_payment = connection.execute(
                "SELECT payment_id FROM payments WHERE order_id = ? AND status = 'paid' "
                "ORDER BY created_at DESC, payment_id DESC LIMIT 1",
                (order_id,),
            ).fetchone()
            payment_created_at = _add_minutes(row["created_at"], 5)
            paid_at = _add_minutes(row["created_at"], 10) if paid else None
            if paid and any_payment is None:
                connection.execute(
                    """
                    INSERT INTO payments (payment_id, order_id, amount, status, paid_at, created_at)
                    VALUES (?, ?, ?, 'paid', ?, ?)
                    ON CONFLICT(payment_id) DO NOTHING
                    """,
                    (payment_id, order_id, row["pay_amount"], paid_at, payment_created_at),
                )
                existing_payment = connection.execute(
                    "SELECT payment_id FROM payments WHERE payment_id = ? AND order_id = ?",
                    (payment_id, order_id),
                ).fetchone()
            if existing_payment is not None:
                payment_id = existing_payment["payment_id"]

            if status in {"shipped", "in_transit", "delivered"}:
                existing_shipment = connection.execute(
                    "SELECT 1 FROM shipments WHERE order_id = ? LIMIT 1",
                    (order_id,),
                ).fetchone()
                if existing_shipment is not None:
                    continue
                shipped_at = row["shipped_at"] or _add_minutes(row["created_at"], 18 * 60)
                delivered_at = row["delivered_at"]
                if status == "delivered" and not delivered_at:
                    delivered_at = _add_minutes(row["created_at"], 3 * 24 * 60)
                shipment_updated_at = delivered_at or shipped_at or row["updated_at"]
                connection.execute(
                    """
                    INSERT INTO shipments (
                        shipment_id, order_id, carrier, tracking_number, status,
                        shipped_at, delivered_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(shipment_id) DO NOTHING
                    """,
                    (
                        f"SHP-{order_id.removeprefix('ORD-')}",
                        order_id,
                        row["courier_company"] or "演示物流",
                        row["tracking_number"] or f"DEMO{order_id.rsplit('-', 1)[-1]}",
                        status,
                        shipped_at,
                        delivered_at,
                        shipment_updated_at,
                    ),
                )

            if status in {"refund_pending", "refunded"}:
                existing_refund = connection.execute(
                    "SELECT 1 FROM refunds WHERE order_id = ? LIMIT 1",
                    (order_id,),
                ).fetchone()
                if existing_refund is not None or existing_payment is None:
                    continue
                refund_status = "completed" if status == "refunded" else "pending"
                requested_at = _add_minutes(row["created_at"], 60)
                completed_at = _add_minutes(row["created_at"], 90) if status == "refunded" else None
                connection.execute(
                    """
                    INSERT INTO refunds (
                        refund_id, order_id, payment_id, amount, reason, status,
                        requested_at, completed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(refund_id) DO NOTHING
                    """,
                    (
                        f"REF-{order_id.removeprefix('ORD-')}",
                        order_id,
                        payment_id,
                        connection.execute(
                            "SELECT amount FROM payments WHERE payment_id = ?",
                            (payment_id,),
                        ).fetchone()[0],
                        "用户申请退款",
                        refund_status,
                        requested_at,
                        completed_at,
                    ),
                )

    @staticmethod
    def _product_summary(items: list[dict[str, Any]]) -> str:
        if not items:
            return "未提供商品信息"
        first = items[0]
        title = f"{first['product_name']} {first['sku_name']}"
        if len(items) == 1:
            return title
        total_quantity = sum(int(item["quantity"]) for item in items)
        return f"{title} 等 {total_quantity} 件商品"


def _add_minutes(value: str, minutes: int) -> str:
    try:
        return (datetime.fromisoformat(value) + timedelta(minutes=minutes)).isoformat(timespec="seconds")
    except (TypeError, ValueError):
        return value
