"""CLI runner for the deterministic offline agent evaluation suite."""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from typing import Any

from evals.models import EvalCaseResult, EvalSummary
from evals.scenarios import ALL_SCENARIOS, EvalScenario, run_scenario


def _report(cases: list[EvalCaseResult]) -> dict[str, Any]:
    return {
        "schema_version": "1",
        "summary": EvalSummary.from_cases(cases).to_dict(),
        "cases": [case.to_dict() for case in cases],
    }


async def run_suite(scenarios: list[EvalScenario] | None = None) -> dict[str, Any]:
    cases: list[EvalCaseResult] = []
    for scenario in ALL_SCENARIOS if scenarios is None else scenarios:
        started = time.perf_counter()
        try:
            checks = await run_scenario(scenario)
            details: dict[str, Any] = {}
        except Exception as exc:
            checks = []
            details = {"exception": f"{type(exc).__name__}: {exc}"}
        duration_ms = (time.perf_counter() - started) * 1000
        cases.append(
            EvalCaseResult(
                case_id=scenario.case_id,
                category=scenario.category,
                passed=bool(checks) and all(check.passed for check in checks),
                critical=scenario.critical,
                checks=checks,
                duration_ms=duration_ms,
                details=details,
            )
        )
    return _report(cases)


def _exit_code(report: dict[str, Any]) -> int:
    return 0 if report["summary"]["failed"] == 0 else 1


def _print_human(report: dict[str, Any]) -> None:
    summary = report["summary"]
    print(
        "Eval summary: "
        f"{summary['passed']}/{summary['total']} passed, "
        f"routing={summary['routing_accuracy']:.3f}, "
        f"side_effect_safety={summary['side_effect_safety_rate']:.3f}, "
        f"failure_containment={summary['failure_containment_rate']:.3f}"
    )
    for case in report["cases"]:
        print(f"{'PASS' if case['passed'] else 'FAIL'} {case['case_id']} ({case['duration_ms']:.1f} ms)")


def main(argv: list[str] | None = None, scenarios: list[EvalScenario] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the offline SmartCS agent eval suite")
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args(argv)
    report = asyncio.run(run_suite(scenarios))
    if args.as_json:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    else:
        _print_human(report)
    return _exit_code(report)


if __name__ == "__main__":
    raise SystemExit(main())
