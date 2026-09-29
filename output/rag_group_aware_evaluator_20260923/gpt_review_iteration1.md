# GPT read-only review, iteration 1

Date: 2026-09-23. Task: `smartcs_group_aware_evaluator_20260923`. Source: [Smartcs-new](https://chatgpt.com/c/6ab0abae-336c-83ee-8d4d-e6afbd0956c0).

Decision: **REVIEW=PASS / GROUP_AWARE_EVALUATOR_ACCEPTED / HOLDOUT_NOT_GOLD**. GPT used the SmartCS read-only workspace (`python-impl / main / HEAD 2148ea1`) to inspect `workspace_info`, the recorded execution summary, changed code, the new test file, and the execution record.

Accepted: first-hit-only fact-group gain at actual unique-chunk rank, group-based Recall and IDCG, alias/canonical equality for MRR, strict group/qrel coverage and documented regrade checks, explicit `--qrel-groups` opt-in, unchanged legacy `evaluate_ranking()` semantics, and 23 targeted / 404 full passing tests (18 skipped). The local optional OpenTelemetry export warning did not change pytest exit 0.

Non-blocking suggestions: add explicit loader tests for unknown or entirely missing query groups, and a `run()`-level branch test for absent versus present `qrel_groups_path`. These were not required for this PASS.

This review does not approve a formal Holdout run. Final human adjudication of the 36 questions and 44 groups, followed by a frozen gold package, remains outstanding.
