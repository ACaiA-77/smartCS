"""Check the first Holdout result's shape without changing its metrics."""

import json
import hashlib
import math
from pathlib import Path


root = Path("artifacts/rag_holdout_v1_first_run_20260923")
gold = Path("benchmarks/rag_holdout_v1")
read = lambda name: json.loads((root / name).read_text(encoding="utf-8"))
model = read("model_validation.json")
overall = read("metrics.json")
by_domain = read("metrics_by_domain.json")
per_query = read("metrics_per_query.json")
variants = {"dense", "bm25", "hybrid_rrf", "hybrid_rerank"}
domains = {"apple_support", "agent_engineering"}
fields = {
    f"{metric}@{cutoff}"
    for metric in ("recall", "mrr", "ndcg", "wrong_domain_rate")
    for cutoff in (1, 3, 5, 10)
}
query_ids = {
    json.loads(line)["query_id"]
    for line in (gold / "queries.jsonl").read_text(encoding="utf-8").splitlines()
}
assert {path.name for path in root.iterdir()} in ({
    "model_validation.json", "metrics.json", "metrics_by_domain.json", "metrics_per_query.json"
}, {
    "model_validation.json", "metrics.json", "metrics_by_domain.json", "metrics_per_query.json",
    "postmortem.json", "postmortem.md",
})
assert model["status"] == "ready" and model["embedding_model"] == "BAAI/bge-m3"
assert model["embedding_dimension"] == 1024
assert model["reranker_model"] == "BAAI/bge-reranker-v2-m3"
assert model["fake_embedding"] is False and model["fake_reranker"] is False
assert set(overall) == variants and set(by_domain) == domains
assert len(per_query) == len(query_ids) == 36
assert {row["query_id"] for row in per_query} == query_ids
protected = json.loads(Path("artifacts/rag_holdout_candidates_20260921/protected_hashes_after.json").read_text(encoding="utf-8"))
index_hashes = {name: expected for name, expected in protected.items() if name.startswith("artifacts/rag_round3/production_indexes/")}
assert index_hashes
assert all(hashlib.sha256(Path(name).read_bytes()).hexdigest() == expected for name, expected in index_hashes.items())


def check_metrics(values):
    assert set(values) == fields
    assert all(math.isfinite(value) and 0 <= value <= 1 for value in values.values())


for values in overall.values():
    check_metrics(values)
for domain in domains:
    assert set(by_domain[domain]) == variants
    for values in by_domain[domain].values():
        check_metrics(values)
for row in per_query:
    assert row["domain"] in domains and row["scoring_mode"] == "fact_group_v1"
    assert row["qrel_group_count"] >= 1 and set(row["variants"]) == variants
    for values in row["variants"].values():
        check_metrics(values)

print(json.dumps({
    "status": "PASS", "queries": len(per_query), "scoring_mode": "fact_group_v1",
    "variants": sorted(variants), "models": "real BGE-M3 + BGE reranker",
    "production_index_files_unchanged_from_candidate_snapshot": len(index_hashes),
}, ensure_ascii=False))
