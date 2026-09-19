from __future__ import annotations

from datetime import datetime

from mcp.order_repository import OrderRepository
from tickets.service import TicketService, canonical_ticket_payload_hash


NOW = datetime(2026, 9, 18, 12, 0, 0)


def _service(tmp_path):
    repository = OrderRepository(str(tmp_path / "orders.db"))
    return repository, TicketService(repository)


def _create(service: TicketService, request_id: str = "client-1", **overrides):
    values = {
        "client_request_id": request_id,
        "user_id": "user-1",
        "title": "服务投诉",
        "description": "需要人工处理服务问题",
        "priority": "high",
        "ticket_type": "complaint",
        "now": NOW,
    }
    values.update(overrides)
    return service.create_ticket(**values)


def test_ticket_is_durable_across_service_instances(tmp_path):
    repository, service = _service(tmp_path)
    created = _create(service)

    restarted = TicketService(OrderRepository(str(repository.db_path)))
    found = restarted.query_ticket(created["ticket_id"], "user-1")

    assert found is not None
    assert found["ticket_id"] == created["ticket_id"]
    assert found["client_request_id"] == "client-1"


def test_same_client_request_same_payload_replays_without_new_row(tmp_path):
    repository, service = _service(tmp_path)
    first = _create(service)
    second = _create(service)

    assert second["success"] is True
    assert second["replayed"] is True
    assert second["ticket_id"] == first["ticket_id"]
    with repository.transaction() as connection:
        assert connection.execute("SELECT COUNT(*) FROM support_tickets").fetchone()[0] == 1


def test_same_client_request_different_payload_is_conflict(tmp_path):
    repository, service = _service(tmp_path)
    first = _create(service)
    conflict = _create(service, title="另一项投诉")

    assert first["success"] is True
    assert conflict["success"] is False
    assert conflict["reason_code"] == "client_request_conflict"
    assert "ticket_id" not in conflict
    assert "status" not in conflict
    with repository.transaction() as connection:
        assert connection.execute("SELECT COUNT(*) FROM support_tickets").fetchone()[0] == 1


def test_client_request_conflict_does_not_disclose_across_users(tmp_path):
    repository, service = _service(tmp_path)
    first = _create(service, request_id="shared-client", user_id="user-1")
    conflict = _create(service, request_id="shared-client", user_id="user-2")

    assert first["success"] is True
    assert conflict == {
        "success": False,
        "reason_code": "client_request_conflict",
        "client_request_id": "shared-client",
    }
    with repository.transaction() as connection:
        assert connection.execute("SELECT COUNT(*) FROM support_tickets").fetchone()[0] == 1


def test_canonical_payload_hash_normalizes_whitespace_and_case():
    first = canonical_ticket_payload_hash(
        user_id=" user-1 ",
        title=" 服务   投诉 ",
        description=" 需要\n人工处理 ",
        priority=" HIGH ",
        category=" Complaint ",
    )
    second = canonical_ticket_payload_hash(
        {
            "user_id": "user-1",
            "title": "服务 投诉",
            "description": "需要 人工处理",
            "priority": "high",
            "category": "complaint",
        }
    )
    assert first == second


def test_caller_payload_hash_mismatch_is_rejected_without_persistence(tmp_path):
    repository, service = _service(tmp_path)
    result = _create(service, request_payload_hash="0" * 64)

    assert result == {
        "success": False,
        "reason_code": "invalid_request_payload_hash",
        "client_request_id": "client-1",
        "user_id": "user-1",
    }
    with repository.transaction() as connection:
        assert connection.execute("SELECT COUNT(*) FROM support_tickets").fetchone()[0] == 0


def test_two_client_request_ids_create_two_tickets(tmp_path):
    repository, service = _service(tmp_path)
    first = _create(service, "client-1")
    second = _create(service, "client-2")

    assert first["ticket_id"] != second["ticket_id"]
    with repository.transaction() as connection:
        assert connection.execute("SELECT COUNT(*) FROM support_tickets").fetchone()[0] == 2


def test_ticket_query_enforces_user_ownership(tmp_path):
    _, service = _service(tmp_path)
    created = _create(service)

    assert service.query_ticket(created["ticket_id"], "other-user") is None
    assert service.query_ticket(created["ticket_id"], "user-1")["user_id"] == "user-1"
