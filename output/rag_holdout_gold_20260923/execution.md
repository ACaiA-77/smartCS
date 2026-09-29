# Holdout gold freeze — execution record

Task `smartcs_holdout_gold_20260923`, iteration 1, 2026-09-23.

User decisions: 36 question texts approved by “我看过了  题目可以通过”; following the explanation of fact groups and relevance grades, “合理” was recorded as approval of the 44-group review sheet, including grades, aliases and the documented Agent 010 regrade. This does not establish corpus-wide qrel completeness or fully independent blindness.

Changed files: `artifacts/rag_holdout_candidates_20260921/human_review_status.md`, `output/rag_holdout_candidates_20260921/make_label_review_sheet.py`, `output/rag_holdout_candidates_20260921/label_review_sheet.md`, `scripts/freeze_rag_holdout.py`, and four files in `benchmarks/rag_holdout_v1/`. Existing Dev benchmark files were not edited in this iteration.

Checks (each exit code 0):

- `python output/rag_holdout_candidates_20260921/review_selection.py check` → `{"status": "PASS", "queries": 36, "qrels": 62, "flagged_reviewed": 25, "gold_frozen": false}` (candidate checker reports its own historical status).
- `python output/rag_holdout_candidates_20260921/dedupe_selection.py check` → `{"status": "PASS", "fact_groups": 44, "aliases": 18, "alias_only_recall_with_current_evaluator": 0.0, "canonical_only_recall_with_current_evaluator": 1.0}`.
- `python -m scripts.freeze_rag_holdout build` → `PASS: rag-holdout-v1 frozen data and source hashes verified`.
- `python -m scripts.freeze_rag_holdout check` → `PASS: rag-holdout-v1 frozen data and source hashes verified`.
- `python -m pytest tests/test_rag_group_aware_evaluator.py -q` → `12 passed in 0.13s`.
- `git diff --check` → exit 0; only existing LF→CRLF warnings.

Gold package: 36 queries, 62 qrels, 44 fact groups, 18 aliases; `fact_group_v1` scoring. The one Agent 010 raw candidate grade 1 was frozen as adjudicated grade 2 with original grade and reason retained. Source chunk SHA-256 matches the candidate generation manifest for both domains. No retrieval, RAG model inference, or Holdout metrics were run in this iteration.

Review request: independently inspect the four gold files, the freeze/check script and this record through the read-only workspace. Check that the user approval scope, hash pinning, group coverage, grade override and Dev isolation meet the planned freeze gate; return PASS/REVISE/BLOCKED and the next bounded step.
