# Group-aware evaluator execution, iteration 1

Task: `smartcs_group_aware_evaluator_20260923`. Workspace: `D:/Workspace_for_Codex/project005_SmartCS/python-impl`. Date: 2026-09-23.

Changed for this task: `rag/evaluation/metrics.py`, `rag/evaluation/evaluator.py`, `scripts/evaluate_rag_retrieval.py`, `tests/test_rag_group_aware_evaluator.py`, and `README.md`. Existing unrelated dirty files under `benchmarks/rag/`, other scripts and tests were not edited. The candidate group and raw qrel files were not changed.

Implementation: `evaluate_grouped_ranking()` credits each fact group at its first actual unique-chunk rank; later aliases occupy their original rank but add no gain. Recall denominator and IDCG use groups. `evaluate_variants()` selects this scorer only with `relevance_groups_by_query`; CLI selects it only with `--qrel-groups`. The loader checks per-query coverage, duplicate members/groups, uniform group grades, and documented raw-qrel regrades. The existing `evaluate_ranking()` body is unchanged.

| Command (all run through `niu -c`) | Exit | Result |
|---|---:|---|
| `python -m pytest tests/test_rag_metrics.py tests/test_rag_benchmark.py -q` (baseline, before edits) | 0 | `11 passed in 1.07s` |
| `python -m pytest tests/test_rag_metrics.py -q` (interim) | 0 | `3 passed in 0.11s` |
| `python -m pytest tests/test_rag_metrics.py tests/test_rag_group_aware_evaluator.py tests/test_rag_benchmark.py -q` (after final edits) | 0 | `23 passed in 1.08s`; includes local 36-query/44-group candidate loading |
| `python -m pytest -q` | 0 | `404 passed, 18 skipped in 156.27s`; after pytest, the optional local OpenTelemetry export reported connection refused at `localhost:4317` |
| `python -m scripts.evaluate_rag_retrieval --help` | 0 | Displays the explicit `--qrel-groups QREL_GROUPS` option |
| `git diff --check` | 0 | No whitespace errors; Git emitted LF-to-CRLF warnings for tracked files |

No real RAG retrieval, BGE/Cross-Encoder run, Holdout metric calculation, gold freeze, or Dev benchmark rewrite was performed.
