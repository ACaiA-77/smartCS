"""Run the opt-in global sparse candidate through the unchanged Dev hybrid pipeline."""

import hashlib
import json
from pathlib import Path

from rag.embeddings import SentenceTransformerEmbeddingBackend
from rag.evaluation.evaluator import evaluate_variants
from rag.evaluation.metrics import aggregate_metrics
from rag.evaluation.report import write_json
from rag.reranker import CrossEncoderReranker
from rag.retriever import HybridRetriever
from scripts.evaluate_rag_retrieval import load_queries, validate_benchmark_manifest
from scripts.validate_rag_models import validate


benchmark = Path("benchmarks/rag")
index_root = Path("artifacts/rag_round3/production_indexes")
baseline_root = Path("artifacts/rag_corrected_baseline_20260921")
counterfactual_path = Path("artifacts/rag_dev_global_bm25_20260923/counterfactual.json")
output = Path("artifacts/rag_global_sparse_candidate_dev_20260923")
digest = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
read = lambda path: json.loads(path.read_text(encoding="utf-8"))


def main():
    validate_benchmark_manifest(benchmark, index_root)
    assert read(benchmark / "benchmark_manifest.json") == read(baseline_root / "benchmark_manifest.json")
    queries = load_queries(benchmark)
    baseline_rows = {row["query_id"]: row for row in read(baseline_root / "metrics_per_query.json")}
    counterfactual = {case["query_id"]: case for case in read(counterfactual_path)["cases"]}
    assert len(queries) == len(baseline_rows) == len(counterfactual) == 60
    assert all((item.query, item.domain, item.kind, item.qrels) ==
               (baseline_rows[item.query_id]["query"], baseline_rows[item.query_id]["domain"],
                baseline_rows[item.query_id]["kind"], baseline_rows[item.query_id]["qrels"])
               for item in queries)
    model_validation = validate(local_only=True)
    if (model_validation.get("status") != "ready" or model_validation.get("fake_embedding") is not False
            or model_validation.get("fake_reranker") is not False):
        raise RuntimeError("real local BGE and Cross-Encoder are required")
    output.mkdir(parents=True, exist_ok=False)
    write_json(output / "model_validation.json", model_validation)
    retriever = HybridRetriever(
        index_root, embedding_backend=SentenceTransformerEmbeddingBackend(),
        reranker=CrossEncoderReranker(), allow_dry_run=False, sparse_mode="global_corpus_v1",
    )
    rows = []
    with (output / "metrics_per_query.jsonl").open("x", encoding="utf-8") as stream:
        for item in queries:
            row = evaluate_variants(retriever, [item])["per_query"][0]
            assert row["rankings"]["dense"] == baseline_rows[item.query_id]["rankings"]["dense"]
            assert row["rankings"]["bm25"] == [hit["chunk_id"] for hit in
                   counterfactual[item.query_id]["global_corpus"]["ranking"]]
            rows.append(row)
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
            stream.flush()
            print(f"{len(rows)}/60 {item.query_id}: dense and global BM25 parity PASS", flush=True)
    variants = ("dense", "bm25", "hybrid_rrf", "hybrid_rerank")
    overall = {variant: aggregate_metrics(row["variants"][variant] for row in rows)
               for variant in variants}
    by_domain = {domain: {variant: aggregate_metrics(row["variants"][variant]
                                                   for row in rows if row["domain"] == domain)
                          for variant in variants}
                 for domain in ("apple_support", "agent_engineering")}
    write_json(output / "metrics_per_query.json", rows)
    write_json(output / "metrics.json", overall)
    write_json(output / "metrics_by_domain.json", by_domain)
    old = read(baseline_root / "metrics.json")
    keys = ("wrong_domain_rate@10", "recall@10", "mrr@10", "ndcg@10")
    comparison = {
        "task_id": "smartcs_global_sparse_candidate_20260923",
        "mode_a": "domain_local_v1 saved Dev baseline",
        "mode_b": "global_corpus_v1 opt-in Dev run",
        "query_count": len(rows),
        "inputs_sha256": {
            "dev_baseline": digest(baseline_root / "metrics_per_query.json"),
            "global_sparse_manifest": digest(index_root / "global_sparse" / "manifest.json"),
            "counterfactual": digest(counterfactual_path),
        },
        "variants": {variant: {key: {"a": old[variant][key], "b": overall[variant][key],
                                     "delta": overall[variant][key] - old[variant][key]}
                               for key in keys}
                     for variant in variants},
        "by_domain_b": by_domain,
        "limitations": "Dev v4 was used for mechanism selection; this run is not unseen validation.",
    }
    write_json(output / "comparison.json", comparison)
    print(json.dumps({"status": "PASS", "query_count": 60,
                      "rrf": comparison["variants"]["hybrid_rrf"],
                      "rerank": comparison["variants"]["hybrid_rerank"]}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
