from __future__ import annotations

from datetime import datetime

import pytest

from mcp.order_repository import OrderRepository
from refunds.service import RefundService
from sandbox.business_simulator import BusinessSimulator


NOW = datetime(2026, 9, 17, 12, 0, 0)


def _service(tmp_path):
    repository = OrderRepository(str(tmp_path / "orders.db"))
    return repository, RefundService(repository)


def test_01_missing_order_is_not_found(tmp_path):
    _, service = _service(tmp_path)
    result = service.evaluate("MISSING", "user_001")
    assert (result.eligible, result.reason_code) == (False, "not_found")


def test_02_wrong_user_is_rejected(tmp_path):
    repository, service = _service(tmp_path)
    result = service.evaluate("ORD-20260801-0002", "user_001")
    assert (result.eligible, result.reason_code) == (False, "order_not_owned")
    assert repository.get_order("ORD-20260801-0002")["user_id"] == "user_002"


def test_03_pending_payment_is_not_paid(tmp_path):
    _, service = _service(tmp_path)
    result = service.evaluate("ORD-20260801-0001", "user_001")
    assert (result.eligible, result.reason_code) == (False, "not_paid")


def test_04_cancelled_order_is_rejected(tmp_path):
    _, service = _service(tmp_path)
    result = service.evaluate("ORD-20260801-0009", "user_009")
    assert (result.eligible, result.reason_code) == (False, "order_cancelled")


def test_05_refunded_order_is_already_refunded(tmp_path):
    _, service = _service(tmp_path)
    result = service.evaluate("ORD-20260801-0008", "user_008")
    assert (result.eligible, result.reason_code) == (False, "already_refunded")


def test_06_seed_refund_pending_is_not_an_eligible_case(tmp_path):
    _, service = _service(tmp_path)
    result = service.evaluate("ORD-20260801-0007", "user_007")
    assert (result.eligible, result.reason_code) == (False, "refund_already_pending")


def test_07_active_refund_blocks_even_when_legacy_order_status_is_paid(tmp_path):
    repository, service = _service(tmp_path)
    with repository.transaction() as connection:
        payment = connection.execute(
            "SELECT payment_id, amount FROM payments WHERE order_id = ? AND status = 'paid'",
            ("ORD-20260801-0002",),
        ).fetchone()
        connection.execute(
            "INSERT INTO refunds VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("CUSTOM-REF-2", "ORD-20260801-0002", payment["payment_id"], payment["amount"], "重复测试", "reviewing", NOW.isoformat(), None),
        )
    result = service.evaluate("ORD-20260801-0002", "user_002")
    assert (result.eligible, result.reason_code) == (False, "refund_already_pending")


@pytest.mark.parametrize(
    ("order_id", "user_id", "mode"),
    [
        ("ORD-20260801-0002", "user_002", "refund_only"),
        ("ORD-20260801-0003", "user_003", "refund_only"),
        ("ORD-20260801-0004", "user_004", "return_and_refund"),
        ("ORD-20260801-0005", "user_005", "return_and_refund"),
        ("ORD-20260801-0006", "user_006", "return_and_refund"),
    ],
)
def test_08_to_12_paid_states_have_the_expected_refund_mode(tmp_path, order_id, user_id, mode):
    _, service = _service(tmp_path)
    result = service.evaluate(order_id, user_id)
    assert (result.eligible, result.reason_code, result.refund_mode) == (True, "eligible", mode)
    assert result.amount > 0


def test_13_evaluate_does_not_write_and_uses_authoritative_payment(tmp_path):
    repository, service = _service(tmp_path)
    before = repository.get_order("ORD-20260801-0002")
    with repository.transaction() as connection:
        connection.execute(
            "UPDATE orders SET payment_status = 'unpaid', pay_amount = 0.01 WHERE order_id = ?",
            ("ORD-20260801-0002",),
        )
    result = service.evaluate("ORD-20260801-0002", "user_002")
    after = repository.get_order("ORD-20260801-0002")
    assert result.eligible and result.amount == before["payment"]["amount"]
    assert after["payment_status"] == "unpaid" and after["refunds"] == []


def test_14_create_refund_writes_one_consistent_transaction(tmp_path):
    repository, service = _service(tmp_path)
    result = service.create_refund("ORD-20260801-0002", "user_002", "商品不符", NOW)
    order = repository.get_order("ORD-20260801-0002")
    assert result.success and result.refund_id == "REF-SIM-000001"
    assert result.requested_at == NOW.isoformat()
    assert order["status"] == "refund_pending"
    assert order["payment_status"] == "paid" and order["after_sale_status"] == "refund_pending"
    assert order["updated_at"] == result.requested_at
    assert order["refunds"][0]["amount"] == order["payment"]["amount"]


def test_15_duplicate_is_restart_safe_and_does_not_insert(tmp_path):
    repository, service = _service(tmp_path)
    first = service.create_refund("ORD-20260801-0002", "user_002", "第一次", NOW)
    second = service.create_refund("ORD-20260801-0002", "user_002", "第二次", NOW)
    restarted = RefundService(OrderRepository(str(repository.db_path)))
    third = restarted.create_refund("ORD-20260801-0002", "user_002", "重启后", NOW)
    assert first.refund_id == second.refund_id == third.refund_id == "REF-SIM-000001"
    assert second.reason_code == third.reason_code == "refund_already_pending"
    assert len(repository.get_order("ORD-20260801-0002")["refunds"]) == 1


def test_16_failure_after_insert_rolls_back_refund_and_order(tmp_path, monkeypatch):
    repository, service = _service(tmp_path)
    before = repository.get_order("ORD-20260801-0002")

    def fail(*_args):
        raise RuntimeError("forced refund failure")

    monkeypatch.setattr(service, "_update_order_legacy", fail)
    with pytest.raises(RuntimeError, match="forced refund failure"):
        service.create_refund("ORD-20260801-0002", "user_002", "回滚", NOW)
    after = repository.get_order("ORD-20260801-0002")
    assert after["status"] == before["status"] == "paid"
    assert after["refunds"] == before["refunds"] == []


def test_17_simulator_completes_created_refund_and_keeps_payment_paid(tmp_path):
    repository, service = _service(tmp_path)
    created = service.create_refund("ORD-20260801-0002", "user_002", "完成测试", NOW)
    BusinessSimulator(repository).tick(now=NOW, max_transitions=1000)
    order = repository.get_order("ORD-20260801-0002")
    assert order["status"] == "refunded"
    assert order["payment"]["status"] == "paid"
    refund = next(refund for refund in order["refunds"] if refund["refund_id"] == created.refund_id)
    assert refund["status"] == "completed"
    assert refund["completed_at"] == NOW.isoformat()
