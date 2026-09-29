"""Verify saved first-run evidence without invoking retrieval or models."""

import json
import math
from pathlib import Path

from rag.evaluation.metrics import aggregate_metrics
from scripts.freeze_rag_cross_domain_sparse_challenge import check as check_challenge
from scripts.validate_rag_cross_domain_sparse_gate import check as check_gate
from output.rag_cross_domain_sparse_challenge_first_ab_20260924.run_ab import gate_report
from scripts.evaluate_rag_retrieval import load_queries, load_relevance_groups


ROOT = Path("artifacts/rag_cross_domain_sparse_challenge_first_ab_20260924")
BENCH = Path("benchmarks/rag_cross_domain_sparse_challenge_v1")
read = lambda name: json.loads((ROOT / name).read_text(encoding="utf-8"))


def main() -> None:
    check_challenge()
    check_gate()
    assert {path.name for path in ROOT.iterdir()} == {
        "preflight.json", "model_validation.json", "arm_A_metrics.json", "arm_A_metrics_by_domain.json",
        "arm_A_metrics_per_query.json", "arm_B_metrics.json", "arm_B_metrics_by_domain.json",
        "arm_B_metrics_per_query.json", "gate_report.json", "comparison.md",
    }
    model, preflight = read("model_validation.json"), read("preflight.json")
    assert model["status"] == "ready" and model["fake_embedding"] is False and model["fake_reranker"] is False
    assert preflight["protected_index_files"] == 10
    queries = load_queries(BENCH)
    groups = load_relevance_groups(BENCH / "qrel_groups.jsonl", queries=queries)
    reports = {}
    for arm in ("A", "B"):
        overall = read(f"arm_{arm}_metrics.json")
        by_domain = read(f"arm_{arm}_metrics_by_domain.json")
        rows = read(f"arm_{arm}_metrics_per_query.json")
        assert [row["query_id"] for row in rows] == [query.query_id for query in queries]
        assert all(row["scoring_mode"] == "fact_group_v1" for row in rows)
        assert set(overall) == {"dense", "bm25", "hybrid_rrf", "hybrid_rerank"}
        for variant, values in overall.items():
            recomputed = aggregate_metrics(row["variants"][variant] for row in rows)
            assert set(values) == set(recomputed)
            assert all(math.isfinite(value) and 0 <= value <= 1 and math.isclose(value, recomputed[key], abs_tol=1e-12)
                       for key, value in values.items())
        for domain, variants in by_domain.items():
            for variant, values in variants.items():
                recomputed = aggregate_metrics(row["variants"][variant] for row in rows if row["domain"] == domain)
                assert all(math.isclose(value, recomputed[key], abs_tol=1e-12) for key, value in values.items())
        reports[arm] = {"overall": overall, "by_domain": by_domain, "per_query": rows}
    assert all(a["rankings"]["dense"] == b["rankings"]["dense"] for a, b in
               zip(reports["A"]["per_query"], reports["B"]["per_query"], strict=True))
    gate = read("gate_report.json")
    expected = gate_report(reports["A"], reports["B"], groups, json.loads((BENCH / "ab_gate.json").read_text(encoding="utf-8")))
    assert {key: gate[key] for key in expected} == expected
    assert gate["dense_parity_queries"] == 24
    print(json.dumps({"status": "PASS", "queries": 24, "decision": gate["decision"], "checks": len(gate["checks"])}, ensure_ascii=False))


if __name__ == "__main__":
    main()
