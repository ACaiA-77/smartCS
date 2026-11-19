"""Generic crash-window reconciliation tests for ExecutionReconciler."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import pytest

from tool_runtime.execution_ledger import ExecutionLedger, canonical_arguments_hash
from tool_runtime.execution_recovery import ExecutionReconciler


def _future() -> datetime:
    return datetime.now(timezone.utc) + timedelta(seconds=120)


def test_stale_write_claim_becomes_manual_required_without_reexecuting(tmp_path) -> None:
    ledger = ExecutionLedger(str(tmp_path / "copilot.db"))
    args = {"account": "163", "to": "alice@example.com", "subject": "Report"}
    assert (
        ledger.claim(
            "stale-mail-send",
            "mail_send",
            canonical_arguments_hash(args),
        ).status
        == "claimed"
    )

    summary = ExecutionReconciler(ledger).reconcile_stale(now=_future())
    assert summary["scanned"] == 1
    assert summary["manual_required"] == 1
    assert summary["recovered_completed"] == 0
    assert summary["released_for_retry"] == 0
    assert summary["items"][0]["outcome"] == "manual_required"
    assert ledger.get("stale-mail-send")["status"] == "in_progress"


def test_reconcile_key_skips_fresh_claim_and_marks_stale_claim(tmp_path) -> None:
    ledger = ExecutionLedger(str(tmp_path / "copilot.db"))
    ledger.claim("fresh-write", "vault_append_daily", {"entry": "hello"})

    reconciler = ExecutionReconciler(ledger)
    outcome, reason = reconciler.reconcile_key("fresh-write")
    assert outcome == "skipped"
    assert "too recent" in reason

    outcome_missing, _ = reconciler.reconcile_key("does-not-exist")
    assert outcome_missing == "skipped"


def test_env_threshold_300_does_not_process_120_second_claim(tmp_path, monkeypatch) -> None:
    ledger = ExecutionLedger(str(tmp_path / "copilot.db"))
    ledger.claim("env-300", "vault_append_daily", {"entry": "配置"})
    monkeypatch.setenv("TOOL_RECOVERY_STALE_SECONDS", "300")

    summary = ExecutionReconciler(ledger).reconcile_stale(now=_future())

    assert summary["scanned"] == 0
    assert ledger.get("env-300")["status"] == "in_progress"


def test_env_threshold_60_processes_same_120_second_claim(tmp_path, monkeypatch) -> None:
    ledger = ExecutionLedger(str(tmp_path / "copilot.db"))
    ledger.claim("env-60", "onto_upsert", {"entity_type": "Project", "name": "Alpha"})
    monkeypatch.setenv("TOOL_RECOVERY_STALE_SECONDS", "60")

    summary = ExecutionReconciler(ledger).reconcile_stale(now=_future())

    assert summary["manual_required"] == 1
    assert ledger.get("env-60")["status"] == "in_progress"


@pytest.mark.parametrize("raw_value", ["0", "-1", "not-a-number"])
def test_invalid_env_threshold_uses_safe_default_and_warns(
    tmp_path, monkeypatch, caplog, raw_value
) -> None:
    ledger = ExecutionLedger(str(tmp_path / "copilot.db"))
    ledger.claim("invalid-env", "wiki_compile_doc", {"title": "Doc"})
    monkeypatch.setenv("TOOL_RECOVERY_STALE_SECONDS", raw_value)

    with caplog.at_level(logging.WARNING, logger="tool_runtime.execution_recovery"):
        summary = ExecutionReconciler(ledger).reconcile_stale(now=_future())

    assert summary["manual_required"] == 1
    assert ledger.get("invalid-env")["status"] == "in_progress"
    assert "Invalid stale recovery threshold" in caplog.text


@pytest.mark.asyncio
async def test_startup_recovery_logs_only_aggregate_counts(monkeypatch, caplog) -> None:
    from api import main as api_main
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock

    monkeypatch.setattr(api_main.PlatformDatabase, "from_env", Mock(return_value=Mock(initialize=AsyncMock())))
    memory_service = SimpleNamespace(initialize=AsyncMock(), start=AsyncMock(), stop=AsyncMock())
    monkeypatch.setattr(api_main, "UserMemoryService", Mock(return_value=memory_service))
    monkeypatch.setattr(api_main, "UserMemoryWorker", Mock(return_value=memory_service))
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

    with caplog.at_level(logging.INFO, logger="api.main"):
        async with api_main.lifespan(api_main.app):
            pass

    assert calls == 1
    memory_service.initialize.assert_awaited_once()
    assert "startup execution recovery" in caplog.text
    assert "scanned=2" in caplog.text
    assert "recovered_completed=1" in caplog.text
    assert "manual_required=1" in caplog.text
