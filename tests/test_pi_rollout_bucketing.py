"""Phase 7 §1: the cohort rollout switch and its deterministic bucketing.

The property under test is stability, not statistics: an account must land in
the same bucket in every process, at every restart. That is precisely what
Python's built-in `hash()` cannot provide (it is salted per process), so the
cross-process case below is the load-bearing one — two subprocesses with
different `PYTHONHASHSEED` values must agree with this process.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from internal_api.harness_client import (
    ROLLOUT_PERCENT_ENV,
    harness_version_for_account,
    rollout_percent,
)

PYTHON_IMPL = Path(__file__).resolve().parents[1]

#: Small, fixed set used by every assertion about determinism.
ACCOUNTS = [1, 2, 3, 7, 42, 99, 100, 1234, 99991, 2**31 - 1]

#: Regression pin: the buckets these accounts fall into at 50%.
PINNED_AT_50 = {
    1: "legacy",
    2: "legacy",
    3: "legacy",
    7: "pi",
    42: "pi",
    99: "pi",
    100: "pi",
    1234: "pi",
    99991: "pi",
    2**31 - 1: "legacy",
}


@pytest.fixture(autouse=True)
def _clean_rollout(monkeypatch):
    """No test inherits another test's rollout setting."""
    monkeypatch.delenv(ROLLOUT_PERCENT_ENV, raising=False)


def test_default_is_zero_legacy_only():
    assert rollout_percent() == 0
    assert {harness_version_for_account(a) for a in ACCOUNTS} == {"legacy"}


@pytest.mark.parametrize("raw,expected", [("0", 0), ("37", 37), ("100", 100), (" 50 ", 50), ("", 0)])
def test_rollout_percent_reads_the_environment(monkeypatch, raw, expected):
    monkeypatch.setenv(ROLLOUT_PERCENT_ENV, raw)
    assert rollout_percent() == expected


@pytest.mark.parametrize("raw", ["-1", "101", "abc", "50.5", "1e2"])
def test_rollout_percent_rejects_a_malformed_value(monkeypatch, raw):
    """A rollout switch that silently reads as 0 is indistinguishable from
    'the rollout is not happening', so a bad value is a hard error."""
    monkeypatch.setenv(ROLLOUT_PERCENT_ENV, raw)
    with pytest.raises(ValueError):
        rollout_percent()


def test_percent_zero_means_every_new_session_is_legacy(monkeypatch):
    monkeypatch.setenv(ROLLOUT_PERCENT_ENV, "0")
    assert {harness_version_for_account(a) for a in range(1, 200)} == {"legacy"}


def test_percent_hundred_means_every_new_session_is_pi(monkeypatch):
    monkeypatch.setenv(ROLLOUT_PERCENT_ENV, "100")
    assert {harness_version_for_account(a) for a in range(1, 200)} == {"pi"}


def test_partial_rollout_produces_both_cohorts(monkeypatch):
    monkeypatch.setenv(ROLLOUT_PERCENT_ENV, "50")
    verdicts = {harness_version_for_account(a) for a in range(1, 200)}
    assert verdicts == {"legacy", "pi"}


@pytest.mark.parametrize("account_id", [0, -1, "1", 1.0, None, True])
def test_account_id_must_be_a_positive_int(account_id):
    with pytest.raises(ValueError):
        harness_version_for_account(account_id)


def test_buckets_are_pinned(monkeypatch):
    """A regression pin: changing the salt or the modulus must be a visible,
    deliberate change, not a silent reshuffle of everyone's cohort."""
    monkeypatch.setenv(ROLLOUT_PERCENT_ENV, "50")
    assert {a: harness_version_for_account(a) for a in ACCOUNTS} == PINNED_AT_50


def test_repeated_calls_never_drift(monkeypatch):
    monkeypatch.setenv(ROLLOUT_PERCENT_ENV, "50")
    first = [harness_version_for_account(a) for a in ACCOUNTS]
    assert [harness_version_for_account(a) for a in ACCOUNTS] == first
    assert [harness_version_for_account(a) for a in reversed(ACCOUNTS)] == list(reversed(first))


def test_a_wider_cohort_only_adds_accounts(monkeypatch):
    """The bucket is one threshold, not a per-percent reshuffle: an account
    admitted at 40% is still admitted at 60%."""
    monkeypatch.setenv(ROLLOUT_PERCENT_ENV, "40")
    at_40 = {a for a in range(1, 500) if harness_version_for_account(a) == "pi"}
    monkeypatch.setenv(ROLLOUT_PERCENT_ENV, "60")
    at_60 = {a for a in range(1, 500) if harness_version_for_account(a) == "pi"}
    assert at_40 < at_60


def test_the_split_is_roughly_uniform(monkeypatch):
    """Sanity, not statistics: a hash that collapsed to 0 or 100 would still
    pass every determinism test above."""
    monkeypatch.setenv(ROLLOUT_PERCENT_ENV, "50")
    share = sum(1 for a in range(1, 2001) if harness_version_for_account(a) == "pi") / 2000
    assert 0.42 <= share <= 0.58


def _verdicts_in_subprocess(seed: str, percent: str) -> dict[str, str]:
    program = (
        "import json;"
        "from internal_api.harness_client import harness_version_for_account as v;"
        f"print(json.dumps({{str(a): v(a) for a in {ACCOUNTS!r}}}))"
    )
    env = {
        **os.environ,
        "PYTHONPATH": str(PYTHON_IMPL),
        ROLLOUT_PERCENT_ENV: percent,
        # The whole point: Python's own hash() would change with this seed.
        "PYTHONHASHSEED": seed,
    }
    result = subprocess.run(
        [sys.executable, "-c", program], env=env, capture_output=True, text=True, timeout=120, check=True
    )
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_bucketing_is_identical_across_processes_and_hash_seeds(monkeypatch):
    """P7-3 (跨进程重启恒定): a fresh interpreter — and one whose string hash
    seed differs — must reach the exact same verdict for every account."""
    monkeypatch.setenv(ROLLOUT_PERCENT_ENV, "50")
    here = {str(a): harness_version_for_account(a) for a in ACCOUNTS}
    assert _verdicts_in_subprocess("0", "50") == here
    assert _verdicts_in_subprocess("12345", "50") == here
    # And the extremes stay extremes in another process.
    assert set(_verdicts_in_subprocess("0", "0").values()) == {"legacy"}
    assert set(_verdicts_in_subprocess("12345", "100").values()) == {"pi"}
