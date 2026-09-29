# First Holdout run: offline postmortem execution

Task `smartcs_holdout_first_run_analysis_20260923`, iteration 1. Follows ChatGPT `REVIEW=PASS / FIRST_FORMAL_HOLDOUT_VALID / BASELINE_FROZEN / NO_LONGER_UNSEEN`.

Input is the previously saved `metrics_per_query.json` (SHA-256 `8271e3af999cf0eb9f63687c579b5bfb2145fd0c88f92b2cd474e1b08d55bfb6`), with the frozen Holdout group mapping and production chunk metadata used only to identify group facts and observed wrong-domain excerpts. No BGE model, reranker, retrieval call, parameter change or second Holdout run occurred.

Command: `python -m output.rag_holdout_first_run_analysis_20260923.analyze` → `{"status": "PASS", "queries": 36, "flag_counts": {"RRF_RECOVERS": 10, "RERANK_RECOVERS_ORDERING": 15, "CROSS_DOMAIN_CONTAMINATION": 21, "FULL_PASS": 19, "FIRST_STAGE_TOP10_WEAKNESS": 3, "RERANK_DROPS_RELEVANT_FACT": 2}}`, exit 0. Flags overlap. Script verifies every reconstructed fact-group Recall@10 equals the saved metric before writing analysis.

Output: `artifacts/rag_holdout_v1_first_run_20260923/postmortem.json` (SHA-256 `dc59921a6561486b0a9e1703523b99e570749d7d30f0cbf9aa517f82d30aa6a2`) and `postmortem.md` (SHA-256 `73083141534b101513b6fbeeae24ef89a7814a02ae7cb3c628d160900b084ac9`). The four original metric/model files remain byte-for-byte unchanged. `python output/rag_holdout_first_formal_run_20260923/verify_run.py` and `git diff --check` both exit 0.

Key findings: rerank drops one grade-1 supporting fact each from Agent017 (RRF rank 5) and Agent035 (RRF rank 10). Agent rerank misses five groups total, across summary caching, asynchronous tool results, rubric criteria, contextual retrieval and training phases. For 18 Apple questions, BM25 has 115 wrong-domain slots out of 180 Top10 slots; 103 are from one Agent book PDF. Shared tokenizer terms and domain-local raw BM25 scores are plausible mechanisms to test on Dev, not proven causes or reasons to retune using this Holdout.

Limit: saved files contain only Top10 rankings. A group missing from all saved Top10 lists may still have been at candidate rank 11–20; the report does not call that a proven first-stage candidate miss. The Holdout has now been seen and cannot serve as an unseen post-optimization baseline.
