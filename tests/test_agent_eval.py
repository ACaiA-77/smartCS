from __future__ import annotations

import json
import socket

import pytest

from evals.models import EvalCaseResult, EvalCheck, EvalSummary
from evals.runner import _exit_code, main, run_suite
from evals.scenarios import (
    ALL_SCENARIOS,
    EvalScenario,
    _ask,
    _ticket_content_disclosure_checks,
    build_runtime,
)


@pytest.mark.asyncio
async def test_default_suite_runs_fully_offline(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts: list[tuple[str, object]] = []

    def blocked(name: str):
        def fail(*args: object, **kwargs: object):
            attempts.append((name, args[0] if args else None))
            raise AssertionError(f"network attempted: {name}")

        return fail

    monkeypatch.setattr(socket.socket, "connect", blocked("connect"))
    monkeypatch.setattr(socket.socket, "connect_ex", blocked("connect_ex"))
    monkeypatch.setattr(socket, "getaddrinfo", blocked("getaddrinfo"))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("MODEL_NAME", raising=False)
    monkeypatch.setenv("EMBEDDING_BACKEND", "openai")
    report = await run_suite()
    assert report["summary"]["failed"] == 0
    assert report["summary"]["pass_rate"] == 1.0
    assert report["summary"]["routing_accuracy"] == 1.0
    assert report["summary"]["side_effect_safety_rate"] == 1.0
    assert report["summary"]["failure_containment_rate"] == 1.0
    assert attempts == []


def test_declared_case_ids_are_unique() -> None:
    case_ids = [scenario.case_id for scenario in ALL_SCENARIOS]
    assert len(case_ids) == len(set(case_ids))


def test_ticket_ownership_invariant_detects_mutated_leak() -> None:
    checks = _ticket_content_disclosure_checks(
        "secret-title",
        "secret-description",
        {"success": True, "title": "secret-title"},
        "secret-description",
    )
    assert any(not check.passed for check in checks)


@pytest.mark.asyncio
async def test_json_report_is_serializable_and_counts_match() -> None:
    report = await run_suite()
    encoded = json.dumps(report, ensure_ascii=False)
    decoded = json.loads(encoded)
    cases = decoded["cases"]
    assert decoded["summary"]["total"] == len(cases)
    assert decoded["summary"]["passed"] == sum(case["passed"] for case in cases)
    assert decoded["summary"]["failed"] == sum(not case["passed"] for case in cases)


def test_failed_critical_case_returns_nonzero() -> None:
    case = EvalCaseResult(
        case_id="failed-critical",
        category="fault",
        passed=False,
        critical=True,
        checks=[EvalCheck("broken", False, True, False)],
    )
    summary = EvalSummary.from_cases([case]).to_dict()
    assert summary["failed"] == 1
    assert _exit_code({"summary": summary}) == 1


@pytest.mark.asyncio
async def test_cases_are_isolated() -> None:
    first = build_runtime()
    second = build_runtime()
    try:
        await _ask(first, "我要投诉服务问题", session_id="shared-session")
        await _ask(first, "帮我退款 ORD-20260801-0002", session_id="shared-session")
        assert (await first.session_store.get_state("shared-session")).pending_action
        assert first.repository.get_order("ORD-20260801-0002")["refunds"] == []
        assert (await second.session_store.get_state("shared-session")).pending_action is None
        assert second.repository.get_order("ORD-20260801-0002")["refunds"] == []
        with second.repository.transaction() as connection:
            assert connection.execute("SELECT COUNT(*) FROM support_tickets").fetchone()[0] == 0
    finally:
        first.close()
        second.close()


def test_runner_main_reports_injected_failure_and_caught_exception(capsys: pytest.CaptureFixture[str]) -> None:
    async def failed_case() -> list[EvalCheck]:
        return [EvalCheck("injected", False, True, False)]

    async def crashing_case() -> list[EvalCheck]:
        raise RuntimeError("synthetic runner failure")

    scenarios = [
        EvalScenario("injected_failure", "test", failed_case),
        EvalScenario("caught_exception", "test", crashing_case),
    ]
    assert main(["--json"], scenarios) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["summary"]["failed"] == 2
    assert report["cases"][1]["details"]["exception"].startswith("RuntimeError:")
