# Frozen RAG test fixtures

These text-only snapshots make benchmark audit, hash preflight, reporting, and stage-diagnosis tests runnable from a clean Git checkout. They are not production indexes and contain no FAISS vectors or model weights.

- `production_chunks/`: byte-identical `chunks.jsonl` and `manifest.json` snapshots from the local frozen `artifacts/rag_round3/production_indexes` baseline. Tests verify the chunk hashes against the checked-in benchmark manifests.
- `corrected_baseline/metrics_per_query.json`: the historical corrected-baseline report used to verify 30 Agent queries and 55 frozen qrels, not a new model evaluation.
- `historical_report/`: historical reporting snapshots used to check that new report output does not overwrite the input evidence.

The source artifacts and benchmark gold files are not rewritten. `.gitattributes` disables newline conversion for these fixtures and benchmarks because their manifests pin raw byte hashes. New experiments belong in separate output directories; do not regenerate these fixtures casually.
