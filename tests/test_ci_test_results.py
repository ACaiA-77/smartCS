"""The CI gate must reject skipped, missing, empty, and failing test runs."""

from __future__ import annotations

import subprocess
import sys
import xml.etree.ElementTree as ET

import pytest

from scripts.check_ci_test_results import ALLOWED_SKIPS, REQUIRED_SUITES, check_report


def report(tmp_path, *, omit=None, outcome=None, suite=None, reason="disabled"):
    root = ET.Element("testsuites")
    tests = ET.SubElement(root, "testsuite")
    for module in REQUIRED_SUITES:
        if module == omit:
            continue
        case = ET.SubElement(tests, "testcase", classname=module, name="integration")
        if outcome and module == suite:
            ET.SubElement(case, outcome, message=reason)
    path = tmp_path / "results.xml"
    ET.ElementTree(root).write(path, encoding="utf-8")
    return path


def test_completed_required_suites_pass(tmp_path):
    assert check_report(report(tmp_path)) == []


@pytest.mark.parametrize("suite", REQUIRED_SUITES)
def test_required_suite_skipped_or_missing_fails(tmp_path, suite):
    skipped = check_report(report(tmp_path, suite=suite, outcome="skipped"))
    assert any("unexpected skip" in problem and suite in problem for problem in skipped)
    missing = check_report(report(tmp_path, omit=suite))
    assert f"missing required suite: {suite}" in missing


@pytest.mark.parametrize("outcome", ["failure", "error"])
def test_failing_results_cannot_pass_gate(tmp_path, outcome):
    problems = check_report(report(tmp_path, suite=REQUIRED_SUITES[0], outcome=outcome))
    assert any("failed test" in problem for problem in problems)


def test_optional_skips_require_both_suite_and_documented_reason(tmp_path):
    path = report(tmp_path)
    tree = ET.parse(path)
    suite = tree.getroot().find("testsuite")
    for module, reason in ALLOWED_SKIPS.items():
        case = ET.SubElement(suite, "testcase", classname=module, name="optional")
        ET.SubElement(case, "skipped", message=f"Skipped: {reason}")
    tree.write(path, encoding="utf-8")
    assert check_report(path) == []
    case = ET.SubElement(suite, "testcase", classname="tests.test_other", name="unrelated")
    ET.SubElement(case, "skipped", message=next(iter(ALLOWED_SKIPS.values())))
    tree.write(path, encoding="utf-8")
    assert any("unexpected skip" in problem for problem in check_report(path))


def test_empty_report_fails(tmp_path):
    path = tmp_path / "empty.xml"
    path.write_text("<testsuites/>", encoding="utf-8")
    assert check_report(path) == ["report contains no test cases"]


@pytest.mark.parametrize("contents", [None, "not XML"])
def test_cli_fails_for_missing_or_invalid_report(tmp_path, contents):
    path = tmp_path / "invalid.xml"
    if contents is not None:
        path.write_text(contents, encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "-m", "scripts.check_ci_test_results", str(path)],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 1
    assert "cannot read test report" in result.stdout
