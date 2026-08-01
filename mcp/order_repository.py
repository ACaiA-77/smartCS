"""SQLite-backed domestic e-commerce demo order repository."""

from __future__ import annotations

import sqlite3
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
            self._seed_demo_orders(connection)
            self._repair_demo_tracking_numbers(connection)
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

        result = dict(row)
        result["items"] = [dict(item) for item in items]
        result["product"] = self._product_summary(result["items"])
        return result

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

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def _create_schema(connection: sqlite3.Connection) -> None:
        connection.executescript(
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
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS order_items (
                item_id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id TEXT NOT NULL,
                product_name TEXT NOT NULL,
                sku_name TEXT NOT NULL,
                unit_price REAL NOT NULL,
                quantity INTEGER NOT NULL,
                item_total REAL NOT NULL,
                FOREIGN KEY(order_id) REFERENCES orders(order_id)
            );
            """
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
    def _repair_demo_tracking_numbers(connection: sqlite3.Connection) -> None:
        """Keep the generated demo database aligned after deterministic seed changes."""
        prefixes = {
            "顺丰速运": "SF",
            "京东物流": "JD",
            "中通快递": "ZT",
            "圆通速递": "YT",
        }
        rows = connection.execute(
            """
            SELECT order_id, courier_company
            FROM orders
            WHERE order_id LIKE 'ORD-20260801-%'
              AND courier_company IS NOT NULL
            """
        ).fetchall()
        for row in rows:
            prefix = prefixes.get(row["courier_company"])
            if prefix is None:
                continue
            suffix = row["order_id"].rsplit("-", 1)[-1]
            connection.execute(
                "UPDATE orders SET tracking_number = ? WHERE order_id = ?",
                (f"{prefix}20260801{int(suffix):04d}", row["order_id"]),
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
