"""Real SQLite transaction and abrupt-process-exit coverage for LocalAtomicWrite."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from tool_runtime.execution_ledger import ExecutionLedger
from tool_runtime.mcp_server import LocalAtomicWrite, MCPToolServer, ToolDefinition
from tool_runtime.tool_execution import ToolExecutionContext, ToolExecutor


class _LocalNoteRepo:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS local_records ("
                "id TEXT PRIMARY KEY, user_id TEXT NOT NULL, note TEXT NOT NULL)"
            )

    def count(self) -> int:
        with sqlite3.connect(self.db_path) as conn:
            return conn.execute("SELECT COUNT(*) FROM local_records").fetchone()[0]


def _register_atomic_tool(server: MCPToolServer, repo: _LocalNoteRepo) -> None:
    def execute_sync(connection: sqlite3.Connection, arguments: dict) -> dict:
        connection.execute(
            "INSERT INTO local_records (id, user_id, note) VALUES (?, ?, ?)",
            (arguments["record_id"], arguments["user_id"], arguments["note"]),
        )
        return {"success": True, "record_id": arguments["record_id"]}

    async def handler(**_kwargs):
        raise AssertionError("atomic writes run synchronously inside ledger transaction")

    server.register_tool(
        ToolDefinition(
            name="atomic_note_write",
            description="atomic local sqlite write",
            input_schema={"type": "object"},
            handler=handler,
            operation_type="write",
            risk_level="medium",
            requires_confirmation=True,
            local_atomic_write=LocalAtomicWrite(
                repository=repo,
                handler=handler,
                execute=execute_sync,
            ),
        )
    )


def _setup(tmp_path):
    db_path = str(tmp_path / "copilot_atomic.db")
    repo = _LocalNoteRepo(db_path)
    server = MCPToolServer()
    _register_atomic_tool(server, repo)
    ledger = ExecutionLedger(db_path)
    executor = ToolExecutor(server, ledger=ledger)
    arguments = {"record_id": "rec-1", "user_id": "user_002", "note": "atomic write"}
    return repo, server, ledger, executor, arguments


def _context(key: str = "atomic-1") -> ToolExecutionContext:
    return ToolExecutionContext(confirmed=True, idempotency_key=key)


@pytest.mark.asyncio
async def test_business_effect_and_exact_result_commit_together(tmp_path, monkeypatch):
    repo, _, ledger, executor, arguments = _setup(tmp_path)
    complete = ledger.complete
    observed = []

    def inspect_completion(key, result, *, connection, expected_claim):
        assert connection.in_transaction
        assert connection.execute("SELECT COUNT(*) FROM local_records").fetchone()[0] == 1
        assert repo.count() == 0
        assert ledger.get(key)["status"] == "in_progress"
        stored = complete(key, result, connection=connection, expected_claim=expected_claim)
        assert (
            connection.execute(
                "SELECT status FROM tool_executions WHERE idempotency_key = ?", (key,)
            ).fetchone()[0]
            == "completed"
        )
        assert ledger.get(key)["status"] == "in_progress"
        assert repo.count() == 0
        observed.append(True)
        return stored

    monkeypatch.setattr(ledger, "complete", inspect_completion)
    result = await executor.execute("atomic_note_write", arguments, _context())
    assert result.success and result.result["success"]
    assert observed == [True]
    assert ledger.get("atomic-1")["result"] == result.as_dict()
    assert repo.count() == 1


@pytest.mark.asyncio
async def test_ledger_completion_error_never_leaves_committed_business_effect(tmp_path, monkeypatch):
    repo, _, ledger, executor, arguments = _setup(tmp_path)

    def fail_completion(*_args, **_kwargs):
        raise RuntimeError("completion persistence failed")

    monkeypatch.setattr(ledger, "complete", fail_completion)
    result = await executor.execute("atomic_note_write", arguments, _context())
    assert result.error_code == "execution_error"
    assert repo.count() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("after_update", [False, True])
async def test_completion_failure_rolls_back_business_and_ledger_update(
    tmp_path, monkeypatch, after_update
):
    repo, _, ledger, executor, arguments = _setup(tmp_path)
    complete = ledger.complete

    def fail_after_update(key, result, *, connection, expected_claim):
        if after_update:
            complete(key, result, connection=connection, expected_claim=expected_claim)
        raise RuntimeError("injected completion failure")

    monkeypatch.setattr(ledger, "complete", fail_after_update)
    result = await executor.execute("atomic_note_write", arguments, _context())
    assert result.error_code == "execution_error"
    assert repo.count() == 0
    assert ledger.get("atomic-1")["status"] == "failed"


@pytest.mark.asyncio
async def test_bypass_or_missing_local_handler_guard_is_rejected(tmp_path):
    repo, server, ledger, executor, arguments = _setup(tmp_path)
    tool = server.get_tool("atomic_note_write")

    async def swapped_handler(**_kwargs):
        return {"bypassed": True}

    server.register_tool(replace(tool, handler=swapped_handler))
    result = await executor.execute("atomic_note_write", arguments, _context())
    assert result.success and result.result == {"bypassed": True}
    assert repo.count() == 0


@pytest.mark.asyncio
async def test_confirmation_in_progress_conflict_and_replay_invariants(tmp_path):
    repo, _, ledger, executor, arguments = _setup(tmp_path)
    unconfirmed = await executor.execute("atomic_note_write", arguments)
    assert unconfirmed.error_code == "confirmation_required"
    assert ledger.get("atomic-1") is None

    ledger.claim("atomic-1", "atomic_note_write", arguments)
    duplicate = await executor.execute("atomic_note_write", arguments, _context())
    conflict = await executor.execute("atomic_note_write", {**arguments, "note": "other"}, _context())
    assert duplicate.error_code == "execution_in_progress"
    assert conflict.error_code == "idempotency_conflict"
    assert repo.count() == 0

    ledger.release("atomic-1")
    first = await executor.execute("atomic_note_write", arguments, _context())
    second = await executor.execute("atomic_note_write", arguments, _context())
    assert first.success and second.replayed and second.result == first.result
    assert repo.count() == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["release", "complete", "replace"])
async def test_claim_is_revalidated_under_write_lock(tmp_path, monkeypatch, change):
    repo, _, ledger, executor, arguments = _setup(tmp_path)
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
    result = await executor.execute("atomic_note_write", arguments, _context())
    assert result.error_code == "execution_claim_lost"
    assert repo.count() == 0
    row = ledger.get("atomic-1")
    if change == "complete":
        assert row["result"] == {"recovered": True}
    elif change == "release":
        assert row is None
    else:
        assert row["status"] == "in_progress"


_CRASH_SCRIPT = """
import asyncio, json, os, sys
from tests.test_atomic_tool_writes import _LocalNoteRepo, _register_atomic_tool
from tool_runtime.execution_ledger import ExecutionLedger
from tool_runtime.mcp_server import MCPToolServer
from tool_runtime.tool_execution import ToolExecutionContext, ToolExecutor

db_path, phase, raw_arguments = sys.argv[1:]
repo = _LocalNoteRepo(db_path)
server = MCPToolServer()
_register_atomic_tool(server, repo)
ledger = ExecutionLedger(db_path)
executor = ToolExecutor(server, ledger=ledger)
complete = ledger.complete
if phase == 'before_commit':
    def crash(*args, **kwargs):
        complete(*args, **kwargs)
        os._exit(73)
    ledger.complete = crash
result = asyncio.run(
    executor.execute(
        'atomic_note_write',
        json.loads(raw_arguments),
        ToolExecutionContext(confirmed=True, idempotency_key='atomic-1'),
    )
)
if not result.success:
    raise RuntimeError(result.error)
os._exit(74)
"""


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["before_commit", "after_commit"])
async def test_hard_process_exit_has_no_partial_commit_and_restart_replays(tmp_path, phase):
    repo, _, ledger, _, arguments = _setup(tmp_path)
    child = subprocess.run(
        [sys.executable, "-c", _CRASH_SCRIPT, str(repo.db_path), phase, json.dumps(arguments)],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert child.returncode == (73 if phase == "before_commit" else 74), child.stderr
    row = ledger.get("atomic-1")
    assert repo.count() == (1 if phase == "after_commit" else 0)
    if phase == "before_commit":
        assert row["status"] == "in_progress" and row["result_json"] is None
        return
    assert row["status"] == "completed"
    saved = json.loads(row["result_json"])

    restarted_repo = _LocalNoteRepo(repo.db_path)
    restarted_server = MCPToolServer()
    _register_atomic_tool(restarted_server, restarted_repo)
    restarted = ToolExecutor(restarted_server, ledger=ExecutionLedger(repo.db_path))
    replay = await restarted.execute("atomic_note_write", arguments, _context())
    assert replay.as_dict() == {**saved, "attempts": 0, "replayed": True}
    assert ledger.get("atomic-1")["result_json"] == row["result_json"]
    assert restarted_repo.count() == 1
