# Holdout run preflight — execution record

Task `smartcs_holdout_run_preflight_20260923`, iteration 1, 2026-09-23. This follows ChatGPT `REVIEW=PASS / HOLDOUT_GOLD_ACCEPTED` for the frozen Gold package.

Changed files in this iteration: `scripts/evaluate_rag_retrieval.py`, `tests/test_rag_holdout_preflight.py`, and a historical wording clarification in `artifacts/rag_holdout_candidates_20260921/human_review_status.md`. The Gold files, retrieval pipeline and existing Dev benchmark were not changed in this iteration.

Preflight behavior: manifest `file_sha256` entries, when present, are checked before model validation; `scoring_mode=fact_group_v1` requires an explicit group path and a match to `qrel_groups_sha256`. The older Dev manifest has no new fields and retains its existing path.

Checks:

- `python -m pytest tests/test_rag_holdout_preflight.py tests/test_rag_group_aware_evaluator.py tests/test_rag_benchmark.py -q` → `24 passed in 1.64s`, exit 0.
- `python -m pytest -q` → `408 passed, 18 skipped in 96.94s`, exit 0. Afterwards the optional OpenTelemetry exporter could not connect to `localhost:4317`; this did not affect pytest status.
- `python -m scripts.freeze_rag_holdout check` → `PASS: rag-holdout-v1 frozen data and source hashes verified`, exit 0.
- `git diff --check` → exit 0; only LF→CRLF warnings in existing files.

No real BGE/Cross-Encoder inference, retrieval, official Holdout run or new Holdout metrics were produced. Request read-only review of the preflight branches and tests; return `REVIEW=PASS/REVISE/BLOCKED` and the next bounded step.
