"""Require the opt-in global sparse path to reproduce the Dev counterfactual."""

import json
import math
from pathlib import Path

from rag.retriever import HybridRetriever
from scripts.evaluate_rag_retrieval import load_queries, validate_benchmark_manifest


root = Path("artifacts/rag_round3/production_indexes")
validate_benchmark_manifest(Path("benchmarks/rag"), root)
expected = json.loads(Path("artifacts/rag_dev_global_bm25_20260923/counterfactual.json").read_text(encoding="utf-8"))
queries = {item.query_id: item for item in load_queries(Path("benchmarks/rag"))}
retriever = HybridRetriever(root, sparse_mode="global_corpus_v1")
assert len(expected["cases"]) == len(queries) == 60
for case in expected["cases"]:
    item = queries[case["query_id"]]
    actual = retriever.sparse_search(item.query, domains=None, top_k=10)
    frozen = case["global_corpus"]["ranking"]
    assert [(hit.chunk_id, hit.domain) for hit in actual] == [(hit["chunk_id"], hit["domain"]) for hit in frozen], item.query_id
    assert all(math.isclose(hit.score, old["score"], rel_tol=0, abs_tol=1e-10)
               for hit, old in zip(actual, frozen)), item.query_id
print(json.dumps({"status": "PASS", "queries": 60, "ranking_parity": 60,
                  "score_tolerance": 1e-10}, ensure_ascii=False))
