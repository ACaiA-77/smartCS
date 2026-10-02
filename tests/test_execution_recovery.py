from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from mcp.execution_ledger import ExecutionLedger, canonical_arguments_hash
from mcp.execution_recovery import ExecutionReconciler
from mcp.mcp_server import MCPToolServer, ToolDefinition, create_default_tools
from mcp.order_repository import OrderRepository
from mcp.tool_execution import ToolExecutionContext, ToolExecutor
from refunds.service import RefundService
from tickets.service import TicketService, canonical_ticket_payload_hash


def _context(key: str) -> ToolExecutionContext:
    return ToolExecutionContext(confirmed=True, idempotency_key=key)


def _fixture(tmp_path):
    repository = OrderRepository(str(tmp_path / "orders.db"))
    service = RefundService(repository)
    ledger = ExecutionLedger(repository.db_path)
    server = create_default_tools(
        MCPToolServer(), order_repository=repository, refund_service=service
    )
    return repository, service, ledger, server


def _claim_refund(ledger: ExecutionLedger, key: str, arguments: dict) -> None:
    assert ledger.claim(
        key,
        "refund_create",
        canonical_arguments_hash(arguments),
        recovery_payload={
            "order_id": arguments["order_id"],
            "user_id": arguments["user_id"],
        },
    ).status == "claimed"


def _future() -> datetime:
    return datetime.now(timezone.utc) + timedelta(seconds=120)


@pytest.mark.asyncio
async def test_refund_effect_is_completed_and_replayed_after_crash(tmp_path) -> None:
    repository, service, ledger, server = _fixture(tmp_path)
    arguments = {
        "order_id": "ORD-20260801-0002",
        "user_id": "user_002",
        "reason": "崩溃窗口",
    }
    _claim_refund(ledger, "recover-refund", arguments)
    created = service.create_refund(**arguments)
    assert created.success is True

    summary = ExecutionReconciler(ledger, service).reconcile_stale(now=_future())
    assert summary["scanned"] == 1
    assert summary["recovered_completed"] == 1
    assert summary["released_for_retry"] == 0
    assert ledger.get("recover-refund")["status"] == "completed"

    replay = ToolExecutor(server, ledger=ledger)
    result = await replay.execute("refund_create", arguments, _context("recover-refund"))
    assert result.success is True and result.replayed is True and result.attempts == 0
    assert result.result["reason_code"] == "reconciled_existing_refund"
    assert len(repository.get_order(arguments["order_id"])["refunds"]) == 1


@pytest.mark.asyncio
async def test_missing_refund_effect_releases_claim_for_one_normal_retry(tmp_path) -> None:
    repository, service, ledger, server = _fixture(tmp_path)
    arguments = {
        "order_id": "ORD-20260801-0002",
        "user_id": "user_002",
        "reason": "重试",
    }
    _claim_refund(ledger, "release-refund", arguments)
    summary = ExecutionReconciler(ledger, service).reconcile_stale(now=_future())
    assert summary["released_for_retry"] == 1
    assert ledger.get("release-refund") is None

    executor = ToolExecutor(server, ledger=ledger)
    first = await executor.execute("refund_create", arguments, _context("release-refund"))
    second = await executor.execute("refund_create", arguments, _context("release-refund"))
    assert first.success is True and second.replayed is True
    assert len(repository.get_order(arguments["order_id"])["refunds"]) == 1


def test_stale_ticket_is_manual_and_never_invokes_handler(tmp_path) -> None:
    repository = OrderRepository(str(tmp_path / "orders.db"))
    service = RefundService(repository)
    ledger = ExecutionLedger(repository.db_path)
    calls = 0

    async def handler(**_kwargs):
        nonlocal calls
        calls += 1
        return {"ticket_id": "TK-1"}

    server = MCPToolServer()
    server.register_tool(
        ToolDefinition(
            name="ticket_create",
            description="ticket",
            input_schema={"type": "object"},
            handler=handler,
            operation_type="write",
            requires_confirmation=True,
        )
    )
    ledger.claim(
        "stale-ticket",
        "ticket_create",
        {"title": "投诉", "description": "服务"},
    )
    summary = ExecutionReconciler(ledger, service).reconcile_stale(now=_future())
    assert summary["manual_required"] == 1
    assert summary["items"][0]["outcome"] == "manual_required"
    assert ledger.get("stale-ticket")["status"] == "in_progress"
    assert calls == 0


def test_old_schema_migrates_and_missing_payload_is_manual(tmp_path) -> None:
    db_path = tmp_path / "legacy.db"
    connection = sqlite3.connect(db_path)
    connection.execute(
        """
        CREATE TABLE tool_executions (
            idempotency_key TEXT PRIMARY KEY,
            tool_name TEXT NOT NULL,
            arguments_hash TEXT NOT NULL,
            status TEXT NOT NULL,
            result_json TEXT,
            error_code TEXT,
            error TEXT,
            operation_type TEXT,
            attempts INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    old_time = "2020-01-01T00:00:00+00:00"
    connection.execute(
        "INSERT INTO tool_executions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("legacy", "refund_create", "hash", "in_progress", None, None, None, "write", 0, old_time, old_time),
    )
    connection.commit()
    connection.close()

    ledger = ExecutionLedger(db_path)
    columns = {row[1] for row in sqlite3.connect(db_path).execute("PRAGMA table_info(tool_executions)")}
    assert "recovery_payload_json" in columns
    assert ledger.get("legacy")["recovery_payload"] is None
    repository = OrderRepository(str(tmp_path / "orders.db"))
    summary = ExecutionReconciler(ledger, RefundService(repository)).reconcile_stale(now=_future())
    assert summary["manual_required"] == 1
    assert ledger.get("legacy")["status"] == "in_progress"


def test_fresh_claim_is_not_processed(tmp_path) -> None:
    _, service, ledger, _ = _fixture(tmp_path)
    ledger.claim("fresh", "refund_create", "hash", {"order_id": "O", "user_id": "U"})
    summary = ExecutionReconciler(ledger, service).reconcile_stale()
    assert summary["scanned"] == 0
    assert ledger.get("fresh")["status"] == "in_progress"


def test_conditional_stale_mutations_do_not_overwrite_race(tmp_path) -> None:
    _, _, ledger, _ = _fixture(tmp_path)
    ledger.claim("race", "refund_create", "hash", {"order_id": "O", "user_id": "U"})
    expected = ledger.get("race")["updated_at"]
    ledger.complete("race", {"success": True, "status": "completed", "attempts": 1})
    assert ledger.complete_stale("race", expected, {"success": True, "attempts": 0}) is False
    assert ledger.release_stale("race", expected) is False
    assert ledger.get("race")["status"] == "completed"


def test_recovery_payload_only_keeps_authoritative_identity(tmp_path) -> None:
    _, _, ledger, _ = _fixture(tmp_path)
    claim = ledger.claim(
        "payload",
        "refund_create",
        "hash",
        {"order_id": "O", "user_id": "U"},
    )
    assert claim.status == "claimed"
    record = ledger.get("payload")
    assert record["recovery_payload"] == {"order_id": "O", "user_id": "U"}


@pytest.mark.asyncio
async def test_ticket_effect_is_completed_and_replayed_after_crash(tmp_path) -> None:
    repository = OrderRepository(str(tmp_path / "orders.db"))
    refund_service = RefundService(repository)
    ticket_service = TicketService(repository)
    ledger = ExecutionLedger(repository.db_path)
    server = create_default_tools(
        MCPToolServer(),
        order_repository=repository,
        ticket_service=ticket_service,
    )
    arguments = {
        "client_request_id": "recover-ticket-client",
        "user_id": "user-1",
        "title": "服务投诉",
        "description": "需要人工处理",
        "priority": "high",
        "category": "complaint",
    }
    arguments["request_payload_hash"] = canonical_ticket_payload_hash(arguments)
    assert ledger.claim(
        "recover-ticket-exec",
        "ticket_create",
        canonical_arguments_hash(arguments),
        recovery_payload={
            "client_request_id": arguments["client_request_id"],
            "user_id": arguments["user_id"],
            "request_payload_hash": arguments["request_payload_hash"],
        },
    ).status == "claimed"
    created = ticket_service.create_ticket(
        client_request_id=arguments["client_request_id"],
        user_id=arguments["user_id"],
        title=arguments["title"],
        description=arguments["description"],
        priority=arguments["priority"],
        ticket_type=arguments["category"],
        request_payload_hash=arguments["request_payload_hash"],
    )
    assert created["success"] is True

    summary = ExecutionReconciler(
        ledger, refund_service, ticket_service=ticket_service
    ).reconcile_stale(now=_future())
    assert summary["recovered_completed"] == 1
    result = await ToolExecutor(server, ledger=ledger).execute(
        "ticket_create",
        arguments,
        _context("recover-ticket-exec"),
    )
    assert result.success is True and result.replayed is True and result.attempts == 0
    with repository.transaction() as connection:
        assert connection.execute("SELECT COUNT(*) FROM support_tickets").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_ticket_missing_effect_releases_for_one_normal_execution(tmp_path) -> None:
    repository = OrderRepository(str(tmp_path / "orders.db"))
    refund_service = RefundService(repository)
    ticket_service = TicketService(repository)
    ledger = ExecutionLedger(repository.db_path)
    server = create_default_tools(
        MCPToolServer(),
        order_repository=repository,
        ticket_service=ticket_service,
    )
    arguments = {
        "client_request_id": "retry-ticket-client",
        "user_id": "user-1",
        "title": "服务投诉",
        "description": "需要人工处理",
        "priority": "medium",
        "category": "complaint",
    }
    arguments["request_payload_hash"] = canonical_ticket_payload_hash(arguments)
    ledger.claim(
        "retry-ticket-exec",
        "ticket_create",
        canonical_arguments_hash(arguments),
        recovery_payload={
            "client_request_id": arguments["client_request_id"],
            "user_id": arguments["user_id"],
            "request_payload_hash": arguments["request_payload_hash"],
        },
    )
    summary = ExecutionReconciler(
        ledger, refund_service, ticket_service=ticket_service
    ).reconcile_stale(now=_future())
    assert summary["released_for_retry"] == 1

    executor = ToolExecutor(server, ledger=ledger)
    first = await executor.execute("ticket_create", arguments, _context("retry-ticket-exec"))
    second = await executor.execute("ticket_create", arguments, _context("retry-ticket-exec"))
    assert first.success is True and second.replayed is True
    with repository.transaction() as connection:
        assert connection.execute("SELECT COUNT(*) FROM support_tickets").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_ticket_recovery_payload_excludes_free_text(tmp_path) -> None:
    repository = OrderRepository(str(tmp_path / "orders.db"))
    ledger = ExecutionLedger(repository.db_path)
    server = create_default_tools(MCPToolServer(), order_repository=repository)
    arguments = {
        "client_request_id": "payload-ticket-client",
        "user_id": "user-1",
        "title": "敏感标题",
        "description": "详细自由文本",
    }
    arguments["request_payload_hash"] = canonical_ticket_payload_hash(arguments)
    result = await ToolExecutor(server, ledger=ledger).execute(
        "ticket_create", arguments, _context("payload-ticket-exec")
    )
    assert result.success is True
    assert ledger.get("payload-ticket-exec")["recovery_payload"] == {
        "client_request_id": "payload-ticket-client",
        "user_id": "user-1",
        "request_payload_hash": arguments["request_payload_hash"],
    }


def test_ticket_recovery_without_payload_hash_remains_manual(tmp_path) -> None:
    repository = OrderRepository(str(tmp_path / "orders.db"))
    ledger = ExecutionLedger(repository.db_path)
    ticket_service = TicketService(repository)
    ledger.claim(
        "ticket-missing-hash",
        "ticket_create",
        "hash",
        {"client_request_id": "client-1", "user_id": "user-1"},
    )

    summary = ExecutionReconciler(
        ledger, RefundService(repository), ticket_service=ticket_service
    ).reconcile_stale(now=_future())

    assert summary["manual_required"] == 1
    assert ledger.get("ticket-missing-hash")["status"] == "in_progress"


@pytest.mark.asyncio
async def test_ticket_recovery_payload_conflict_completes_terminally_without_disclosure(tmp_path) -> None:
    repository = OrderRepository(str(tmp_path / "orders.db"))
    refund_service = RefundService(repository)
    ticket_service = TicketService(repository)
    ledger = ExecutionLedger(repository.db_path)
    server = create_default_tools(
        MCPToolServer(),
        order_repository=repository,
        ticket_service=ticket_service,
    )
    stored = {
        "client_request_id": "shared-client",
        "user_id": "user-1",
        "title": "原始投诉",
        "description": "原始内容",
        "priority": "high",
        "category": "complaint",
    }
    stored["request_payload_hash"] = canonical_ticket_payload_hash(stored)
    created = ticket_service.create_ticket(**{
        "client_request_id": stored["client_request_id"],
        "user_id": stored["user_id"],
        "title": stored["title"],
        "description": stored["description"],
        "priority": stored["priority"],
        "ticket_type": stored["category"],
        "request_payload_hash": stored["request_payload_hash"],
    })
    assert created["success"] is True

    conflicting = dict(stored)
    conflicting["title"] = "另一项投诉"
    conflicting["request_payload_hash"] = canonical_ticket_payload_hash(conflicting)
    assert ledger.claim(
        "ticket-conflict-recovery",
        "ticket_create",
        canonical_arguments_hash(conflicting),
        recovery_payload={
            "client_request_id": conflicting["client_request_id"],
            "user_id": conflicting["user_id"],
            "request_payload_hash": conflicting["request_payload_hash"],
        },
    ).status == "claimed"

    summary = ExecutionReconciler(
        ledger, refund_service, ticket_service=ticket_service
    ).reconcile_stale(now=_future())
    assert summary["recovered_completed"] == 1
    record = ledger.get("ticket-conflict-recovery")
    assert record["status"] == "completed"
    assert record["result"]["result"] == {
        "success": False,
        "reason_code": "client_request_conflict",
        "client_request_id": "shared-client",
    }
    assert "ticket_id" not in record["result"]["result"]

    replay = await ToolExecutor(server, ledger=ledger).execute(
        "ticket_create", conflicting, _context("ticket-conflict-recovery")
    )
    assert replay.success is True and replay.replayed is True
    assert replay.result["success"] is False
    assert "ticket_id" not in replay.result
    with repository.transaction() as connection:
        assert connection.execute("SELECT COUNT(*) FROM support_tickets").fetchone()[0] == 1


def test_env_threshold_300_does_not_process_120_second_claim(tmp_path, monkeypatch) -> None:
    _, service, ledger, _ = _fixture(tmp_path)
    _claim_refund(
        ledger,
        "env-300",
        {"order_id": "ORD-20260801-0002", "user_id": "user_002", "reason": "配置"},
    )
    monkeypatch.setenv("TOOL_RECOVERY_STALE_SECONDS", "300")

    summary = ExecutionReconciler(ledger, service).reconcile_stale(now=_future())

    assert summary["scanned"] == 0
    assert ledger.get("env-300")["status"] == "in_progress"


def test_env_threshold_60_processes_same_120_second_claim(tmp_path, monkeypatch) -> None:
    _, service, ledger, _ = _fixture(tmp_path)
    arguments = {
        "order_id": "ORD-20260801-0002",
        "user_id": "user_002",
        "reason": "配置",
    }
    _claim_refund(ledger, "env-60", arguments)
    assert service.create_refund(**arguments).success is True
    monkeypatch.setenv("TOOL_RECOVERY_STALE_SECONDS", "60")

    summary = ExecutionReconciler(ledger, service).reconcile_stale(now=_future())

    assert summary["recovered_completed"] == 1
    assert ledger.get("env-60")["status"] == "completed"


@pytest.mark.parametrize("raw_value", ["0", "-1", "not-a-number"])
def test_invalid_env_threshold_uses_safe_default_and_warns(
    tmp_path, monkeypatch, caplog, raw_value
) -> None:
    _, service, ledger, _ = _fixture(tmp_path)
    _claim_refund(
        ledger,
        "invalid-env",
        {"order_id": "ORD-20260801-0002", "user_id": "user_002", "reason": "配置"},
    )
    monkeypatch.setenv("TOOL_RECOVERY_STALE_SECONDS", raw_value)

    with caplog.at_level(logging.WARNING, logger="mcp.execution_recovery"):
        summary = ExecutionReconciler(ledger, service).reconcile_stale(now=_future())

    assert summary["released_for_retry"] == 1
    assert ledger.get("invalid-env") is None
    assert "Invalid stale recovery threshold" in caplog.text


@pytest.mark.asyncio
async def test_startup_recovery_logs_only_aggregate_counts(monkeypatch, caplog) -> None:
    from api import main as api_main
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock

    # Lifecycle logging test must not connect to MySQL because a real .env exists.
    monkeypatch.setattr(api_main.CheckpointStore, "from_env", Mock(return_value=Mock(initialize=AsyncMock())))
    monkeypatch.setattr(api_main.PlatformDatabase, "from_env", Mock(return_value=Mock(initialize=AsyncMock())))
    memory_service = SimpleNamespace(initialize=AsyncMock())
    monkeypatch.setattr(api_main, "UserMemoryService", Mock(return_value=memory_service))
    monkeypatch.setattr(api_main, "issue_token", Mock(return_value="component-only-not-a-jwt"))

    summary = {
        "scanned": 2,
        "recovered_completed": 1,
        "released_for_retry": 0,
        "manual_required": 1,
        "skipped": 0,
    }
    calls = 0

    class StubReconciler:
        def reconcile_stale(self):
            nonlocal calls
            calls += 1
            return summary

    monkeypatch.setattr(api_main, "execution_reconciler", StubReconciler())
    monkeypatch.setattr(
        api_main,
        "create_chat_orchestrator",
        lambda **_kwargs: object(),
    )

    with caplog.at_level(logging.INFO, logger="api.main"):
        async with api_main.lifespan(api_main.app):
            pass

    assert calls == 1
    memory_service.initialize.assert_awaited_once()
    assert "startup execution recovery" in caplog.text
    assert "scanned=2" in caplog.text
    assert "recovered_completed=1" in caplog.text
    assert "manual_required=1" in caplog.text
