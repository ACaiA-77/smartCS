"""Acceptance checks for the deterministic business simulator."""

from __future__ import annotations

import sqlite3
import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from mcp.order_repository import OrderRepository
from sandbox.business_simulator import BusinessSimulator


TABLES = ("users", "orders", "order_items", "payments", "shipments", "refunds")


def _snapshot(path: Path) -> dict[str, list[tuple]]:
    with sqlite3.connect(path) as connection:
        return {
            table: [tuple(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY rowid")]
            for table in TABLES
        }


def _run_full(path: Path, now: datetime | None = None):
    repository = OrderRepository(str(path))
    return repository, BusinessSimulator(repository).tick(now=now, max_transitions=1000)


def test_all_legal_state_transitions_and_detail_side_effects(tmp_path: Path) -> None:
    path = tmp_path / "orders.db"
    repository, result = _run_full(path, datetime(2026, 9, 17, 12, 0, 0))
    transitions = {(item.from_status, item.to_status) for item in result.transitions}
    assert {
        ("pending_payment", "paid"),
        ("pending_payment", "cancelled"),
        ("paid", "processing"),
        ("processing", "shipped"),
        ("shipped", "in_transit"),
        ("in_transit", "delivered"),
        ("refund_pending", "refunded"),
    } <= transitions
    for transition in result.transitions:
        order = repository.get_order(transition.order_id)
        assert order["status"] == transition.to_status
        if transition.to_status == "shipped":
            assert order["shipment"] and order["shipment"]["status"] == "shipped"
            assert order["courier_company"] == order["shipment"]["carrier"]
            assert order["tracking_number"] == order["shipment"]["tracking_number"]
            assert order["shipped_at"] == order["shipment"]["shipped_at"]
        if transition.to_status == "delivered":
            assert order["delivered_at"] == order["shipment"]["delivered_at"]
        if transition.to_status == "refunded":
            assert order["payment"]["status"] == "paid"
            assert any(refund["status"] == "completed" for refund in order["refunds"])


def test_existing_payment_is_reconciled_to_order_amount_when_paid(tmp_path: Path) -> None:
    repository = OrderRepository(str(tmp_path / "orders.db"))
    with repository.transaction() as connection:
        connection.execute(
            "UPDATE orders SET pay_amount = 123.45, updated_at = '0000' WHERE order_id = ?",
            ("ORD-20260801-0001",),
        )
        connection.execute(
            """
            INSERT INTO payments (payment_id, order_id, amount, status, paid_at, created_at)
            VALUES ('CUSTOM-PAY-1', 'ORD-20260801-0001', 1, 'requested', NULL, '0000')
            """
        )
    simulator = BusinessSimulator(repository)
    simulator._should_cancel = lambda _order_id: False
    now = datetime(2026, 9, 17, 12, 0, 0)
    result = simulator.tick(now=now, max_transitions=1)
    assert result.transitions[0].order_id == "ORD-20260801-0001"
    payment = repository.get_order("ORD-20260801-0001")["payment"]
    assert payment["amount"] == 123.45
    assert payment["status"] == "paid"
    assert payment["paid_at"] == now.isoformat(timespec="seconds")


@pytest.mark.parametrize("refund_status", ["pending", "requested", "reviewing", "approved", "processing"])
def test_refund_completion_accepts_supported_lifecycle_statuses(tmp_path: Path, refund_status: str) -> None:
    repository = OrderRepository(str(tmp_path / "orders.db"))
    with repository.transaction() as connection:
        connection.execute(
            "UPDATE orders SET updated_at = '0000' WHERE order_id = ?", ("ORD-20260801-0007",)
        )
        connection.execute(
            "UPDATE refunds SET status = ? WHERE order_id = ?", (refund_status, "ORD-20260801-0007")
        )
    now = datetime(2026, 9, 17, 12, 0, 0)
    result = BusinessSimulator(repository).tick(now=now, max_transitions=1)
    assert result.transitions[0].to_status == "refunded"
    refund = repository.get_order("ORD-20260801-0007")["refunds"][0]
    assert refund["status"] == "completed"
    assert refund["completed_at"] == now.isoformat(timespec="seconds")


def test_pending_payment_cancel_is_deterministic_and_has_no_successful_payment(tmp_path: Path) -> None:
    first = tmp_path / "first.db"
    second = tmp_path / "second.db"
    _, first_result = _run_full(first, datetime(2026, 9, 17, 12, 0, 0))
    second_repo, second_result = _run_full(second, datetime(2026, 9, 17, 12, 0, 0))
    first_cancelled = sorted(item.order_id for item in first_result.transitions if item.to_status == "cancelled")
    second_cancelled = sorted(item.order_id for item in second_result.transitions if item.to_status == "cancelled")
    assert first_cancelled == second_cancelled
    assert first_cancelled
    for order_id in first_cancelled:
        order = second_repo.get_order(order_id)
        assert order["payment"] is None or order["payment"]["status"] != "paid"


def test_refund_updates_legacy_after_sale_but_keeps_payment_paid(tmp_path: Path) -> None:
    repository = OrderRepository(str(tmp_path / "orders.db"))
    result = BusinessSimulator(repository).tick(
        now=datetime(2026, 9, 17, 12, 0, 0), max_transitions=1000
    )
    refunded = next(item.order_id for item in result.transitions if item.to_status == "refunded")
    order = repository.get_order(refunded)
    assert order["payment"]["status"] == "paid"
    assert order["payment_status"] == "refunded"
    assert order["after_sale_status"] == "refunded"
    assert order["after_sale_status_label"] == "退款完成"


def test_delivered_orders_do_not_start_refunds(tmp_path: Path) -> None:
    repository = OrderRepository(str(tmp_path / "orders.db"))
    before = repository.get_order("ORD-20260801-0006")
    BusinessSimulator(repository).tick(now=datetime(2026, 9, 17, 12, 0, 0), max_transitions=1000)
    after = repository.get_order("ORD-20260801-0006")
    assert before["status"] == after["status"] == "delivered"
    assert after["refunds"] == before["refunds"] == []


def test_max_transitions_and_one_step_per_order(tmp_path: Path) -> None:
    repository = OrderRepository(str(tmp_path / "orders.db"))
    result = BusinessSimulator(repository).tick(
        now=datetime(2026, 9, 17, 12, 0, 0), max_transitions=3
    )
    assert result.transition_count == 3
    assert len({item.order_id for item in result.transitions}) == 3
    assert all((item.from_status, item.to_status) in {
        ("pending_payment", "paid"),
        ("pending_payment", "cancelled"),
        ("paid", "processing"),
        ("processing", "shipped"),
        ("shipped", "in_transit"),
        ("in_transit", "delivered"),
        ("refund_pending", "refunded"),
    } for item in result.transitions)


def test_fairness_moves_past_the_first_batch(tmp_path: Path) -> None:
    repository = OrderRepository(str(tmp_path / "orders.db"))
    simulator = BusinessSimulator(repository)
    first = simulator.tick(now=datetime(2026, 9, 17, 12, 0, 0), max_transitions=2)
    second = simulator.tick(now=datetime(2026, 9, 17, 12, 1, 0), max_transitions=2)
    assert set(item.order_id for item in first.transitions).isdisjoint(
        item.order_id for item in second.transitions
    )


def test_fixed_now_makes_fresh_database_results_identical(tmp_path: Path) -> None:
    now = datetime(2026, 9, 17, 12, 0, 0)
    first = tmp_path / "first.db"
    second = tmp_path / "second.db"
    _run_full(first, now)
    _run_full(second, now)
    assert _snapshot(first) == _snapshot(second)


def test_create_orders_have_only_legacy_order_and_item_rows(tmp_path: Path) -> None:
    path = tmp_path / "orders.db"
    repository = OrderRepository(str(path))
    result = BusinessSimulator(repository).tick(
        now=datetime(2026, 9, 17, 12, 0, 0), max_transitions=0, create_orders=2
    )
    assert result.created_order_ids == ("ORD-SIM-000001", "ORD-SIM-000002")
    assert all(re.fullmatch(r"ORD[-_][A-Z0-9-]+", order_id) for order_id in result.created_order_ids)
    for order_id in result.created_order_ids:
        order = repository.get_order(order_id)
        assert order["status"] == "pending_payment"
        assert order["payment"] is None and order["shipment"] is None and order["refunds"] == []
        assert order["items"]


def test_create_order_ids_survive_restart_without_collision(tmp_path: Path) -> None:
    path = tmp_path / "orders.db"
    repository = OrderRepository(str(path))
    simulator = BusinessSimulator(repository)
    simulator.tick(max_transitions=0, create_orders=2, now=datetime(2026, 9, 17, 12, 0, 0))
    restarted = BusinessSimulator(OrderRepository(str(path)))
    result = restarted.tick(max_transitions=0, create_orders=1, now=datetime(2026, 9, 17, 12, 1, 0))
    assert result.created_order_ids == ("ORD-SIM-000003",)
    assert restarted.repository.count_orders() == 103


def test_tick_rolls_back_all_writes_on_transition_failure(tmp_path: Path) -> None:
    path = tmp_path / "orders.db"
    repository = OrderRepository(str(path))
    simulator = BusinessSimulator(repository)
    before = _snapshot(path)
    original = simulator._transition

    def fail_after_write(connection, row, timestamp):
        original(connection, row, timestamp)
        raise RuntimeError("forced transition failure")

    simulator._transition = fail_after_write
    with pytest.raises(RuntimeError, match="forced transition failure"):
        simulator.tick(
            now=datetime(2026, 9, 17, 12, 0, 0), max_transitions=1, create_orders=1
        )
    assert _snapshot(path) == before


def test_all_mutations_in_one_tick_use_the_supplied_now(tmp_path: Path) -> None:
    now = datetime(2026, 9, 17, 12, 0, 0)
    repository = OrderRepository(str(tmp_path / "orders.db"))
    result = BusinessSimulator(repository).tick(now=now, max_transitions=1, create_orders=1)
    timestamp = now.isoformat(timespec="seconds")
    with repository.transaction() as connection:
        created_row = connection.execute(
            "SELECT created_at, updated_at FROM orders WHERE order_id = ?", (result.created_order_ids[0],)
        ).fetchone()
        assert tuple(created_row) == (timestamp, timestamp)
        order_id = result.transitions[0].order_id
        assert connection.execute("SELECT updated_at FROM orders WHERE order_id = ?", (order_id,)).fetchone()[0] == timestamp


def test_transaction_public_boundary_commits_and_rolls_back(tmp_path: Path) -> None:
    repository = OrderRepository(str(tmp_path / "orders.db"))
    with repository.transaction() as connection:
        connection.execute("UPDATE orders SET status_label = '事务测试' WHERE order_id = 'ORD-20260801-0001'")
    assert repository.get_order("ORD-20260801-0001")["status_label"] == "事务测试"
    with pytest.raises(RuntimeError):
        with repository.transaction() as connection:
            connection.execute("UPDATE orders SET status_label = '应回滚' WHERE order_id = 'ORD-20260801-0001'")
            raise RuntimeError("rollback")
    assert repository.get_order("ORD-20260801-0001")["status_label"] == "事务测试"


def test_cli_ticks_one_exits_with_summary(tmp_path: Path) -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.run_business_simulator",
            "--db-path",
            str(tmp_path / "cli.db"),
            "--ticks",
            "1",
            "--interval",
            "0",
            "--max-transitions",
            "1",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert "tick=1" in completed.stdout
    assert "transitions=1" in completed.stdout


def test_invalid_tick_limits_are_rejected(tmp_path: Path) -> None:
    simulator = BusinessSimulator(OrderRepository(str(tmp_path / "orders.db")))
    with pytest.raises(ValueError):
        simulator.tick(max_transitions=-1)
    with pytest.raises(ValueError):
        simulator.tick(create_orders=-1)


def test_transition_result_is_structured_and_serializable(tmp_path: Path) -> None:
    result = BusinessSimulator(OrderRepository(str(tmp_path / "orders.db"))).tick(
        now=datetime(2026, 9, 17, 12, 0, 0), max_transitions=1
    )
    assert result.transition_count == 1
    assert result.transitions[0]["order_id"]
    assert result.as_dict()["transition_count"] == 1
