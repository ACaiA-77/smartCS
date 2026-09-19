"""Acceptance checks for the SQLite business sandbox order dataset."""

from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from mcp.order_repository import OrderRepository


TABLES = ("users", "orders", "order_items", "payments", "shipments", "refunds")


def _snapshot(path: Path, table: str) -> list[tuple]:
    with sqlite3.connect(path) as connection:
        return [tuple(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY rowid")]


def _counts(path: Path) -> dict[str, int]:
    with sqlite3.connect(path) as connection:
        return {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in TABLES
        }


def test_schema_counts_and_scenarios(tmp_path: Path) -> None:
    path = tmp_path / "orders.db"
    repository = OrderRepository(str(path))

    assert _counts(path) == {
        "users": 20,
        "orders": 100,
        "order_items": 133,
        "payments": 77,
        "shipments": 33,
        "refunds": 22,
    }
    with sqlite3.connect(path) as connection:
        names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert set(TABLES) <= names
    assert {repository.get_order(f"ORD-20260801-{index:04d}")["status"] for index in range(1, 10)} == {
        "pending_payment",
        "paid",
        "processing",
        "shipped",
        "in_transit",
        "delivered",
        "refund_pending",
        "refunded",
        "cancelled",
    }


def test_initialize_is_idempotent_and_preserves_evolved_rows(tmp_path: Path) -> None:
    path = tmp_path / "orders.db"
    repository = OrderRepository(str(path))
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE users SET display_name = '保留用户' WHERE user_id = 'user_001'")
        connection.execute("UPDATE orders SET tracking_number = '保留旧运单' WHERE order_id = 'ORD-20260801-0004'")
        connection.execute("UPDATE payments SET status = 'manual_review' WHERE payment_id = 'PAY-20260801-0002'")
        connection.execute("UPDATE shipments SET tracking_number = '保留新运单' WHERE shipment_id = 'SHP-20260801-0004'")
        connection.execute("UPDATE refunds SET reason = '保留退款原因' WHERE refund_id = 'REF-20260801-0007'")
        connection.commit()
    before = {table: _snapshot(path, table) for table in TABLES}

    assert repository.initialize() == 100
    assert {table: _snapshot(path, table) for table in TABLES} == before


def test_initialize_keeps_authoritative_custom_detail_ids(tmp_path: Path) -> None:
    path = tmp_path / "orders.db"
    repository = OrderRepository(str(path))
    with sqlite3.connect(path) as connection:
        connection.execute("DELETE FROM refunds WHERE order_id IN ('ORD-20260801-0007', 'ORD-20260801-0008')")
        connection.execute("DELETE FROM shipments WHERE order_id = 'ORD-20260801-0004'")
        connection.execute("DELETE FROM payments WHERE order_id IN ('ORD-20260801-0002', 'ORD-20260801-0007', 'ORD-20260801-0008')")
        connection.executemany(
            "INSERT INTO payments (payment_id, order_id, amount, status, paid_at, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            [
                ("CUSTOM-PAY-2", "ORD-20260801-0002", 1, "manual_review", None, "2026-08-01T10:00:00"),
                ("CUSTOM-PAY-7", "ORD-20260801-0007", 1, "paid", "2026-08-01T10:10:00", "2026-08-01T10:00:00"),
                ("CUSTOM-PAY-8", "ORD-20260801-0008", 1, "paid", "2026-08-01T10:10:00", "2026-08-01T10:00:00"),
            ],
        )
        connection.execute(
            "INSERT INTO shipments VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("CUSTOM-SHP-4", "ORD-20260801-0004", "保留物流", "保留运单", "shipped", "2026-08-01T10:00:00", None, "2026-08-01T10:00:00"),
        )
        connection.execute(
            "INSERT INTO refunds VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("CUSTOM-REF-7", "ORD-20260801-0007", "CUSTOM-PAY-7", 1, "保留原因", "pending", "2026-08-01T11:00:00", None),
        )
        connection.execute(
            "INSERT INTO refunds VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("CUSTOM-REF-8", "ORD-20260801-0008", "CUSTOM-PAY-8", 1, "保留原因", "completed", "2026-08-01T11:00:00", "2026-08-01T12:00:00"),
        )
        connection.commit()
    before = {table: _snapshot(path, table) for table in TABLES}

    assert repository.initialize() == 100
    assert {table: _snapshot(path, table) for table in TABLES} == before


def test_refund_backfill_uses_authoritative_payment_amount(tmp_path: Path) -> None:
    path = tmp_path / "orders.db"
    repository = OrderRepository(str(path))
    with sqlite3.connect(path) as connection:
        connection.execute("DELETE FROM refunds WHERE order_id = 'ORD-20260801-0008'")
        connection.execute("DELETE FROM payments WHERE order_id = 'ORD-20260801-0008'")
        connection.execute(
            "INSERT INTO payments VALUES (?, ?, ?, ?, ?, ?)",
            ("CUSTOM-PAY-8", "ORD-20260801-0008", 1, "paid", "2026-08-01T10:10:00", "2026-08-01T10:00:00"),
        )
        connection.commit()

    repository.initialize()
    order = repository.get_order("ORD-20260801-0008")
    assert order["payment"]["payment_id"] == "CUSTOM-PAY-8"
    assert len(order["refunds"]) == 1
    assert order["refunds"][0]["payment_id"] == "CUSTOM-PAY-8"
    assert order["refunds"][0]["amount"] == 1


def test_nonpaid_authoritative_payment_does_not_create_payment_or_refund(tmp_path: Path) -> None:
    path = tmp_path / "orders.db"
    repository = OrderRepository(str(path))
    with sqlite3.connect(path) as connection:
        connection.execute("DELETE FROM refunds WHERE order_id = 'ORD-20260801-0007'")
        connection.execute("DELETE FROM payments WHERE order_id = 'ORD-20260801-0007'")
        connection.execute(
            "INSERT INTO payments VALUES (?, ?, ?, ?, ?, ?)",
            ("CUSTOM-PAY-7", "ORD-20260801-0007", 1, "pending", None, "2026-08-01T10:00:00"),
        )
        connection.commit()

    before = {table: _snapshot(path, table) for table in TABLES}
    repository.initialize()
    assert {table: _snapshot(path, table) for table in TABLES} == before
    assert repository.get_order("ORD-20260801-0007")["refunds"] == []


def test_fresh_databases_are_deterministic(tmp_path: Path) -> None:
    first = tmp_path / "first.db"
    second = tmp_path / "second.db"
    OrderRepository(str(first))
    OrderRepository(str(second))

    for table in TABLES:
        assert _snapshot(first, table) == _snapshot(second, table)


def test_business_invariants_and_detail_enrichment(tmp_path: Path) -> None:
    path = tmp_path / "orders.db"
    repository = OrderRepository(str(path))
    orders = [repository.get_order(f"ORD-20260801-{index:04d}") for index in range(1, 101)]
    assert all(orders)

    statuses = {order["status"] for order in orders}
    assert {"pending_payment", "paid", "shipped", "in_transit", "delivered", "refund_pending", "refunded", "cancelled"} <= statuses
    for order in orders:
        payment = order["payment"]
        shipment = order["shipment"]
        refunds = order["refunds"]
        if order["status"] in {"pending_payment", "cancelled"}:
            assert payment is None or payment["status"] != "paid"
        if order["status"] in {"paid", "processing", "shipped", "in_transit", "delivered", "refund_pending", "refunded"}:
            assert payment is not None and payment["status"] == "paid"
        if order["status"] in {"shipped", "in_transit", "delivered"}:
            assert shipment is not None
        if order["status"] == "delivered":
            assert shipment["delivered_at"] is not None
        if order["status"] == "refunded":
            assert any(refund["status"] == "completed" and refund["completed_at"] for refund in refunds)
        for refund in refunds:
            assert refund["order_id"] == order["order_id"] == payment["order_id"]
            assert refund["payment_id"] == payment["payment_id"]
            assert refund["amount"] <= payment["amount"]

    pending = repository.get_order("ORD-20260801-0001")
    paid = repository.get_order("ORD-20260801-0002")
    assert pending["payment"] is None and pending["shipment"] is None and pending["refunds"] == []
    assert paid["payment"] and paid["shipment"] is None and paid["refunds"] == []
    assert all(set(order) >= {"items", "product", "payment", "shipment", "refunds"} for order in orders)

    with sqlite3.connect(path) as connection:
        expected = [
            tuple(row)
            for row in connection.execute(
                "SELECT order_id, status, status_label, pay_amount, created_at FROM orders "
                "ORDER BY created_at DESC LIMIT 20"
            )
        ]
    assert repository.list_orders(limit=20) == [
        dict(zip(("order_id", "status", "status_label", "pay_amount", "created_at"), row))
        for row in expected
    ]


def test_old_schema_migration_preserves_orders_and_items(tmp_path: Path) -> None:
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE orders (
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
            CREATE TABLE order_items (
                item_id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id TEXT NOT NULL,
                product_name TEXT NOT NULL,
                sku_name TEXT NOT NULL,
                unit_price REAL NOT NULL,
                quantity INTEGER NOT NULL,
                item_total REAL NOT NULL,
                FOREIGN KEY(order_id) REFERENCES orders(order_id)
            );
            INSERT INTO orders VALUES (
                'LEGACY-1', 'legacy-user', 'delivered', '人工修改状态', 'paid', '已支付',
                100, 10, 90, '赵*', '130****0000', '南京市', '旧物流',
                '保留旧运单', NULL, NULL, '无', '无',
                '2026-08-01T10:00:00', '2026-08-01T10:20:00'
            );
            INSERT INTO order_items VALUES (1, 'LEGACY-1', '保留商品', '保留规格', 90, 1, 90);
            """
        )
        connection.commit()

    repository = OrderRepository(str(path))
    order = repository.get_order("LEGACY-1")
    assert repository.count_orders() == 101
    assert order["status_label"] == "人工修改状态"
    assert order["tracking_number"] == "保留旧运单"
    assert order["items"] == [
        {"product_name": "保留商品", "sku_name": "保留规格", "unit_price": 90.0, "quantity": 1, "item_total": 90.0}
    ]
    assert order["payment"]["amount"] == 90.0
    assert order["delivered_at"] is None
    assert order["shipment"]["shipped_at"] == "2026-08-02T04:00:00"
    assert order["shipment"]["delivered_at"] == "2026-08-04T10:00:00"
    assert order["shipment"]["updated_at"] == "2026-08-04T10:00:00"


def test_connection_closes_and_failed_initialization_rolls_back(tmp_path: Path) -> None:
    path = tmp_path / "orders.db"
    repository = object.__new__(OrderRepository)
    repository.db_path = path
    connection_holder: list[sqlite3.Connection] = []
    with repository._connect() as connection:
        connection_holder.append(connection)
    with pytest.raises(sqlite3.ProgrammingError):
        connection_holder[0].execute("SELECT 1")

    def fail(_connection: sqlite3.Connection) -> None:
        raise RuntimeError("seed failed")

    repository._seed_order_details = fail
    with pytest.raises(RuntimeError, match="seed failed"):
        repository.initialize()
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table'"
        ).fetchone()[0] == 0


def test_init_script_describes_sandbox_and_prints_six_table_counts(tmp_path: Path) -> None:
    path = tmp_path / "script.db"
    completed = subprocess.run(
        [sys.executable, "-m", "scripts.init_demo_orders", "--db-path", str(path)],
        check=True,
        capture_output=True,
        text=True,
    )
    assert "business sandbox" in completed.stdout
    assert "users=20" in completed.stdout
    assert "orders=100" in completed.stdout
    assert all(f"{table}=" in completed.stdout for table in TABLES)
