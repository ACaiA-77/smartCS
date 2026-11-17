"""Reject empty CI reports and silent loss of required integration coverage."""

from __future__ import annotations

import argparse
from pathlib import Path
import xml.etree.ElementTree as ET


REQUIRED_SUITES = (
    "tests.test_auth",
    "tests.test_user_memory",
    "tests.test_user_sessions",
    "tests.test_pi_unified_entry_e2e",
)
# These depend on optional model runtimes or untracked evaluation artifacts.
# jieba is a required dependency, so its absence must fail this gate.
ALLOWED_SKIPS = {
    "tests.test_rag_rerank_truncation": "opt-in ONNX backend not installed",
    "tests.test_rag_group_aware_evaluator": "local holdout candidate artifacts are not checked in",
}


def check_report(path: Path) -> list[str]:
    """Return coverage failures from an actual pytest JUnit XML report."""
    root = ET.parse(path).getroot()
    cases = list(root.iter("testcase"))
    if not cases:
        return ["report contains no test cases"]

    failures = []
    seen = set()
    for case in cases:
        module = case.get("classname", "")
        name = case.get("name", "")
        for suite in REQUIRED_SUITES:
            if module == suite or module.startswith(suite + "."):
                seen.add(suite)
        if case.find("failure") is not None or case.find("error") is not None:
            failures.append(f"failed test: {module}::{name}")
        skipped = case.find("skipped")
        if skipped is not None:
            reason = skipped.get("message", "")
            allowed_reason = ALLOWED_SKIPS.get(module)
            if allowed_reason is None or allowed_reason not in reason:
                failures.append(f"unexpected skip: {module}::{name}: {reason}")
    failures.extend(f"missing required suite: {suite}" for suite in REQUIRED_SUITES if suite not in seen)
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    args = parser.parse_args()
    try:
        failures = check_report(args.report)
    except (OSError, ET.ParseError) as exc:
        failures = [f"cannot read test report: {exc}"]
    if failures:
        print("CI integration coverage: FAIL")
        for failure in failures:
            print(f"- {failure}")
        return 1
    print("CI integration coverage: PASS (required suites ran; only documented optional skips)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
