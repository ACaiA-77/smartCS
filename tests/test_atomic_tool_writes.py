"""Real SQLite transaction and abrupt-process-exit coverage for local writes."""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from auth.context import UserContext, current_user
from tool_runtime.execution_ledger import ExecutionLedger
from tool_runtime.mcp_server import MCPToolServer, create_default_tools
from tool_runtime.order_repository import OrderRepository
from tool_runtime.tool_execution import ToolExecutionContext, ToolExecutor
from tickets.service import canonical_ticket_payload_hash


def _setup(tmp_path, kind):
    repository = OrderRepository(str(tmp_path / "business.db"))
    server = create_default_tools(MCPToolServer(), order_repository=repository)
    ledger = ExecutionLedger(repository.db_path)
    return repository, server, ledger, ToolExecutor(server, ledger=ledger), _arguments(kind)


def _arguments(kind):
    if kind == "refund":
        return {"order_id": "ORD-20260801-0002", "user_id": "user_002", "reason": "退货"}
    arguments = {
        "client_request_id": "atomic-client-1", "user_id": "user_002",
        "title": "投诉", "description": "请人工处理",
    }
    arguments["request_payload_hash"] = canonical_ticket_payload_hash(arguments)
    return arguments


def _context():
    return ToolExecutionContext(confirmed=True, idempotency_key="atomic-1")


def _effect(connection, kind):
    if kind == "refund":
        refunds = connection.execute(
            "SELECT COUNT(*) FROM refunds WHERE order_id = 'ORD-20260801-0002'"
        ).fetchone()[0]
        status = connection.execute(
            "SELECT status FROM orders WHERE order_id = 'ORD-20260801-0002'"
        ).fetchone()[0]
        return refunds, status
    return connection.execute("SELECT COUNT(*) FROM support_tickets").fetchone()[0]


def _assert_effect(repository, kind, present):
    expected = ((1, "refund_pending") if present else (0, "paid")) if kind == "refund" else int(present)
    with repository.transaction() as connection:
        assert _effect(connection, kind) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["refund", "ticket"])
async def test_business_effect_and_exact_result_commit_together(tmp_path, monkeypatch, kind):
    repository, _, ledger, executor, arguments = _setup(tmp_path, kind)
    complete = ledger.complete
    observed = []

    def inspect_completion(key, result, *, connection, expected_claim):
        assert connection.in_transaction
        assert _effect(connection, kind) == ((1, "refund_pending") if kind == "refund" else 1)
        _assert_effect(repository, kind, False)
        assert ledger.get(key)["status"] == "in_progress"
        stored = complete(key, result, connection=connection, expected_claim=expected_claim)
        assert connection.execute(
            "SELECT status FROM tool_executions WHERE idempotency_key = ?", (key,)
        ).fetchone()[0] == "completed"
        assert ledger.get(key)["status"] == "in_progress"
        _assert_effect(repository, kind, False)
        observed.append(True)
        return stored

    monkeypatch.setattr(ledger, "complete", inspect_completion)
    result = await executor.execute(f"{kind}_create", arguments, _context())
    assert result.success and result.result["success"]
    assert observed == [True]
    assert ledger.get("atomic-1")["result"] == result.as_dict()
    _assert_effect(repository, kind, True)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["refund", "ticket"])
async def test_ledger_completion_error_never_leaves_committed_business_effect(tmp_path, monkeypatch, kind):
    """Uses only pre-fix APIs so the old crash window fails at the effect assertion."""
    repository, _, ledger, executor, arguments = _setup(tmp_path, kind)

    def fail_completion(*_args, **_kwargs):
        raise RuntimeError("completion persistence failed")

    monkeypatch.setattr(ledger, "complete", fail_completion)
    try:
        result = await executor.execute(f"{kind}_create", arguments, _context())
    except RuntimeError as exc:
        assert str(exc) == "completion persistence failed"
    else:
        assert result.error_code == "execution_error"
    _assert_effect(repository, kind, False)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["refund", "ticket"])
@pytest.mark.parametrize("after_update", [False, True])
async def test_completion_failure_rolls_back_business_and_ledger_update(
    tmp_path, monkeypatch, kind, after_update,
):
    repository, _, ledger, executor, arguments = _setup(tmp_path, kind)
    complete = ledger.complete

    def fail_completion(*args, **kwargs):
        assert _effect(kwargs["connection"], kind) == (
            (1, "refund_pending") if kind == "refund" else 1
        )
        if after_update:
            complete(*args, **kwargs)
        raise RuntimeError("completion persistence failed")

    monkeypatch.setattr(ledger, "complete", fail_completion)
    result = await executor.execute(f"{kind}_create", arguments, _context())
    assert result.error_code == "execution_error"
    assert result.error == "completion persistence failed"
    _assert_effect(repository, kind, False)
    assert ledger.get("atomic-1")["status"] == "failed"
    replay = await executor.execute(f"{kind}_create", arguments, _context())
    assert replay.replayed and replay.error == result.error


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["refund", "ticket"])
@pytest.mark.parametrize("failure", ["handler", "serialization"])
async def test_failure_after_business_sql_rolls_back(tmp_path, kind, failure):
    repository, server, ledger, executor, arguments = _setup(tmp_path, kind)
    tool = server.get_tool(f"{kind}_create")
    original = tool.local_atomic_write

    def fail_after_business(connection, values):
        original.execute(connection, values)
        assert _effect(connection, kind) == ((1, "refund_pending") if kind == "refund" else 1)
        if failure == "handler":
            raise RuntimeError("failed after business SQL")
        return {"unencodable": object()}

    tool.local_atomic_write = replace(original, execute=fail_after_business)
    result = await executor.execute(tool.name, arguments, _context())
    assert result.error_code == (
        "execution_error" if failure == "handler" else "result_not_serializable"
    )
    _assert_effect(repository, kind, False)
    assert ledger.get("atomic-1")["status"] == "failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["refund", "ticket"])
async def test_mismatched_database_fails_before_business_work(tmp_path, kind):
    repository, server, _, _, arguments = _setup(tmp_path, kind)
    ledger = ExecutionLedger(tmp_path / "wrong.db")
    executor = ToolExecutor(server, ledger=ledger)
    result = await executor.execute(f"{kind}_create", arguments, _context())
    assert not result.success and "database to match" in result.error
    _assert_effect(repository, kind, False)
    assert ledger.get("atomic-1")["status"] == "failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["refund", "ticket"])
@pytest.mark.parametrize("field", ["connection", "transaction", "_connection"])
async def test_tool_arguments_cannot_supply_transaction(tmp_path, kind, field):
    repository, _, ledger, executor, arguments = _setup(tmp_path, kind)
    result = await executor.execute(f"{kind}_create", {**arguments, field: "injected"}, _context())
    assert result.error_code == "execution_error"
    _assert_effect(repository, kind, False)
    assert ledger.get("atomic-1")["status"] == "failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["refund", "ticket"])
async def test_local_business_rejection_is_completed_and_replayed(tmp_path, kind):
    repository, _, ledger, executor, arguments = _setup(tmp_path, kind)
    if kind == "refund":
        arguments["user_id"] = "other"
        reason = "order_not_owned"
    else:
        arguments["request_payload_hash"] = "invalid"
        reason = "invalid_request_payload_hash"
    result = await executor.execute(f"{kind}_create", arguments, _context())
    assert result.success and result.status == "completed"
    assert result.result["success"] is False and result.result["reason_code"] == reason
    assert ledger.get("atomic-1")["status"] == "completed"
    replay = await executor.execute(f"{kind}_create", arguments, _context())
    assert replay.replayed and replay.result == result.result
    _assert_effect(repository, kind, False)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["refund", "ticket"])
async def test_duplicate_conflict_in_progress_and_identity_checks(tmp_path, kind):
    repository, _, ledger, executor, arguments = _setup(tmp_path, kind)
    name = f"{kind}_create"
    unconfirmed = await executor.execute(name, arguments)
    assert unconfirmed.error_code == "confirmation_required"
    assert ledger.get("atomic-1") is None
    ledger.claim("atomic-1", name, arguments)
    duplicate = await executor.execute(name, arguments, _context())
    conflict = await executor.execute(name, {**arguments, "user_id": "other"}, _context())
    assert duplicate.error_code == "execution_in_progress"
    assert conflict.error_code == "idempotency_conflict"
    _assert_effect(repository, kind, False)
    ledger.release("atomic-1")
    first = await executor.execute(name, arguments, _context())
    second = await executor.execute(name, arguments, _context())
    assert first.success and second.replayed and second.result == first.result
    token = current_user.set(UserContext(1, "another", "other"))
    try:
        denied = await executor.execute(name, arguments, _context())
    finally:
        current_user.reset(token)
    assert denied.error_code == "user_identity_mismatch" and not denied.replayed
    _assert_effect(repository, kind, True)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["refund", "ticket"])
@pytest.mark.parametrize("change", ["release", "complete", "replace"])
async def test_claim_is_revalidated_under_write_lock(tmp_path, monkeypatch, kind, change):
    repository, _, ledger, executor, arguments = _setup(tmp_path, kind)
    claim = ledger.claim

    def race(*args, **kwargs):
        claimed = claim(*args, **kwargs)
        if change == "complete":
            ledger.complete("atomic-1", {"recovered": True})
        else:
            ledger.release("atomic-1")
            if change == "replace":
                claim(*args, **kwargs)
        return claimed

    monkeypatch.setattr(ledger, "claim", race)
    result = await executor.execute(f"{kind}_create", arguments, _context())
    assert result.error_code == "execution_claim_lost"
    _assert_effect(repository, kind, False)
    row = ledger.get("atomic-1")
    if change == "complete":
        assert row["result"] == {"recovered": True}
    elif change == "release":
        assert row is None
    else:
        assert row["status"] == "in_progress"


_CRASH_SCRIPT = """
import asyncio, json, os, sys
from tool_runtime.execution_ledger import ExecutionLedger
from tool_runtime.mcp_server import MCPToolServer, create_default_tools
from tool_runtime.order_repository import OrderRepository
from tool_runtime.tool_execution import ToolExecutionContext, ToolExecutor
db_path, name, phase, raw_arguments = sys.argv[1:]
repository = OrderRepository(db_path)
ledger = ExecutionLedger(db_path)
executor = ToolExecutor(create_default_tools(MCPToolServer(), order_repository=repository), ledger=ledger)
complete = ledger.complete
if phase == 'before_commit':
    def crash(*args, **kwargs):
        complete(*args, **kwargs)
        os._exit(73)
    ledger.complete = crash
result = asyncio.run(executor.execute(name, json.loads(raw_arguments), ToolExecutionContext(confirmed=True, idempotency_key='atomic-1')))
if not result.success:
    raise RuntimeError(result.error)
os._exit(74)
"""


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["refund", "ticket"])
@pytest.mark.parametrize("phase", ["before_commit", "after_commit"])
async def test_hard_process_exit_has_no_partial_commit_and_restart_replays(tmp_path, kind, phase):
    repository, _, ledger, _, arguments = _setup(tmp_path, kind)
    child = subprocess.run(
        [sys.executable, "-c", _CRASH_SCRIPT, str(repository.db_path), f"{kind}_create",
         phase, json.dumps(arguments)],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=30,
    )
    assert child.returncode == (73 if phase == "before_commit" else 74), child.stderr
    row = ledger.get("atomic-1")
    _assert_effect(repository, kind, phase == "after_commit")
    if phase == "before_commit":
        assert row["status"] == "in_progress" and row["result_json"] is None
        return
    assert row["status"] == "completed"
    saved = json.loads(row["result_json"])
    restarted = ToolExecutor(
        create_default_tools(MCPToolServer(), order_repository=OrderRepository(str(repository.db_path))),
        ledger=ExecutionLedger(repository.db_path),
    )
    replay = await restarted.execute(f"{kind}_create", arguments, _context())
    assert replay.as_dict() == {**saved, "attempts": 0, "replayed": True}
    assert ledger.get("atomic-1")["result_json"] == row["result_json"]
    _assert_effect(repository, kind, True)
