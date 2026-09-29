# First formal Holdout run — execution record

Task `smartcs_holdout_first_formal_run_20260923`, iteration 1. ChatGPT had accepted Gold and run preflight, then explicitly approved this first run. No retrieval, scoring, model or Gold parameters were changed after inspecting results; this is the first formal measurement on Holdout v1.

- Local time: start `2026-09-23T20:56:13+08:00`; completion observed `2026-09-23T21:25:23+08:00`.
- Workspace: `D:/Workspace_for_Codex/project005_SmartCS/python-impl`, branch `main`, HEAD `2148ea187436ff359a0f767a18377388690df469`.
- The checkout was already dirty before this run, including `benchmarks/rag/*` and evaluator work from prior tasks. `git status --short` was captured before and after; no tracked status changes occurred during the run. The output and this audit record are new untracked artifacts.
- Pre-run gates: output directory absent (`test ! -e artifacts/rag_holdout_v1_first_run_20260923`, exit 0); `python -m scripts.freeze_rag_holdout check` → `PASS: rag-holdout-v1 frozen data and source hashes verified`, exit 0; `python -m pytest tests/test_rag_holdout_preflight.py tests/test_rag_group_aware_evaluator.py -q` → `16 passed in 0.31s`, exit 0.
- Exact run command: `python -m scripts.evaluate_rag_retrieval --benchmark-root benchmarks/rag_holdout_v1 --artifact-root artifacts/rag_round3/production_indexes --qrel-groups benchmarks/rag_holdout_v1/qrel_groups.jsonl --output-root artifacts/rag_holdout_v1_first_run_20260923` (executed through `niu -c`). Exit 0; stdout `{"status": "ready", "output_root": "artifacts\\rag_holdout_v1_first_run_20260923"}`. Model load emitted only `get_sentence_embedding_dimension` deprecation warnings.
- Model validation: `status=ready`, `embedding_model=BAAI/bge-m3`, dimension `1024`, `reranker_model=BAAI/bge-reranker-v2-m3`, `fake_embedding=false`, `fake_reranker=false`.
- Post-run verifier: `python output/rag_holdout_first_formal_run_20260923/verify_run.py` → `PASS`: exactly four output files, four variants, two domains, 36 distinct query rows, `scoring_mode=fact_group_v1` on every row, required bounded finite metric fields. All 10 production index files matched the preexisting candidate snapshot hashes. `python -m scripts.freeze_rag_holdout check` still passed.

Gold manifest hashes: `queries.jsonl=27ffc0757c7b4bc591d4de8bf66df9f306f13a4076deb912d4bbdf030c386d57`; `qrels.jsonl=9f6d492ac8f039853b82f4d004cbcb30ac8cc607b9b1f438e4b48cd589e2dbcf`; `qrel_groups.jsonl=43ee5908533095795d49db847c997db46982a1f25ed1930aeb980f1151efa72e`. Source chunks: `agent_engineering=d4b73fa26871ccf46077e91ac5363ce83fd922ba9c95232719dff1061568bd75`; `apple_support=bf0ce82c6414dee55bd6a7b69365b37d4322d89407dea48e66c3662b378b455d`.

Evaluator file SHA-256 before and after run, identical: `rag/evaluation/metrics.py=c1e6e6fd218c2098722fb806ea1c9964fc18f6a9b077a6d0bf14d25f985dbf16`; `rag/evaluation/evaluator.py=10cff893360ef6660c19a43e008ba4eaafc84f9140bc73d02ae483c1100afe49`; `scripts/evaluate_rag_retrieval.py=cce56b1d516fef93ecad69505a532fa55f7b47e7173d91d86e77a67f2b97379a`.

Original overall @10 metrics from `metrics.json` (no tuning or threshold applied):

| Variant | Recall@10 | MRR@10 | nDCG@10 | Wrong-domain rate@10 |
|---|---:|---:|---:|---:|
| bm25 | 0.8194444444444444 | 0.6502314814814816 | 0.6783894440867547 | 0.3277777777777778 |
| dense | 0.8472222222222222 | 0.6371472663139329 | 0.6713038994023209 | 0.008333333333333335 |
| hybrid_rrf | 0.9444444444444444 | 0.7079805996472663 | 0.7465263621494768 | 0.14722222222222223 |
| hybrid_rerank | 0.9166666666666666 | 0.8518518518518519 | 0.8628732543814868 | 0.030555555555555558 |

The complete unrounded original outputs are the four files in `artifacts/rag_holdout_v1_first_run_20260923/`. Their SHA-256: `model_validation.json=879638494b058e7b8356872e01f82ef318c553dc3634551fbf80fa0d94578eb3`; `metrics.json=abec615569c8c49238cf0f7ee5b4182c5706339083655f652a1943e399314ed9`; `metrics_by_domain.json=a4acefaca77fc5cc87ccadfcfb1240026e63951c6f2c295a4d127c33f504e704`; `metrics_per_query.json=8271e3af999cf0eb9f63687c579b5bfb2145fd0c88f92b2cd474e1b08d55bfb6`.

Interpretation limit: candidate authoring was partially blind and qrel completeness is not established corpus-wide. This run is valid against the frozen Gold contract; metric magnitude is separate from validity. The same Holdout must not be rerun after retrieval tuning and described as an unseen baseline.
