"""Freeze and verify the challenge A/B decision rule without running retrieval."""

import argparse
import hashlib
import json
from pathlib import Path

from scripts.freeze_rag_cross_domain_sparse_challenge import check as check_challenge


ROOT = Path("benchmarks/rag_cross_domain_sparse_challenge_v1")
GATE = ROOT / "ab_gate.json"
INDEX = Path("artifacts/rag_round3/production_indexes")
LOCKED_FILES = (
    ROOT / "benchmark_manifest.json",
    ROOT / "queries.jsonl",
    ROOT / "qrels.jsonl",
    ROOT / "qrel_groups.jsonl",
    INDEX / "global_sparse/manifest.json",
    INDEX / "global_sparse/bm25_index.json",
    INDEX / "apple_support/manifest.json",
    INDEX / "agent_engineering/manifest.json",
    *(Path(name) for name in (
        "rag/build.py", "rag/dense_retriever.py", "rag/sparse_retriever.py",
        "rag/global_sparse.py", "rag/retriever.py", "rag/fusion.py",
        "rag/reranker.py", "rag/embeddings.py", "rag/evaluation/evaluator.py",
        "rag/evaluation/metrics.py", "scripts/evaluate_rag_retrieval.py",
        "scripts/validate_rag_cross_domain_sparse_gate.py",
    )),
)
PROTOCOL = {
    "gate_version": "global-sparse-ab-gate-v1",
    "benchmark_version": "rag-cross-domain-sparse-challenge-v1",
    "benchmark_split": "challenge",
    "scoring_mode": "fact_group_v1",
    "arms": {"A": "domain_local_v1", "B": "global_corpus_v1"},
    "pipeline": {
        "dense_model": "BAAI/bge-m3", "dense_top_k": 20,
        "sparse_top_k": 20, "rrf_k": 60, "rrf_top_k": 20,
        "ce_model": "BAAI/bge-reranker-v2-m3", "ce_top_k": 10,
        "query_rewrite": False, "domain_oracle": False,
        "query_domain_filter": None,
    },
    "metric_contract": {
        "aggregation": "macro_mean_over_queries_in_scope",
        "wrong_domain_rate_at_10": "wrong-domain hits divided by returned hits, then query macro mean",
        "grade_2_loss": "per-query grade-2 fact groups hit by A at CE10 but absent from B at CE10",
        "threshold_unit": "absolute_fraction",
    },
    "gates": {
        "sparse_mechanism": {
            "bm25_decoy_wrong_domain_rate@10": {
                "scope": "cross_domain_decoy_queries", "normal": "B <= A - 0.10",
                "if_A_below_0.10": "B <= A",
            },
            "bm25_recall@10": {"scope": "all_24", "rule": "B >= A - 0.02"},
        },
        "rrf": {
            "hybrid_rrf_recall@10": {"scope": "all_24", "rule": "B >= A"},
            "hybrid_rrf_wrong_domain_rate@10": {"scope": "all_24", "rule": "B <= A"},
        },
        "final_ce": {
            "hybrid_rerank_recall@10": {"scope": "all_24", "rule": "B >= A"},
            "hybrid_rerank_wrong_domain_rate@10": {"scope": "all_24", "rule": "B <= A"},
            "hybrid_rerank_mrr@10": {"scope": "all_24", "rule": "B >= A - 0.01"},
            "hybrid_rerank_ndcg@10": {"scope": "all_24", "rule": "B >= A - 0.01"},
            "newly_lost_grade_2_fact_groups@ce10": {"scope": "all_24", "rule": "count == 0"},
        },
        "domain_safety": {
            "apple_support_hybrid_rerank_recall@10": {"scope": "apple_support", "rule": "B >= A - 0.02"},
            "agent_engineering_hybrid_rerank_recall@10": {"scope": "agent_engineering", "rule": "B >= A - 0.02"},
        },
    },
    "observed_only": ["bm25_mrr@10", "bm25_ndcg@10", "hybrid_rrf_mrr@10", "hybrid_rrf_ndcg@10"],
    "decision": {
        "rule": "all_required_gates_must_pass",
        "pass": "GLOBAL_SPARSE_DEFAULT_ELIGIBLE",
        "fail": "KEEP_DOMAIN_LOCAL_DEFAULT",
        "no_metric_tradeoffs": True,
        "default_change_in_this_task": False,
    },
    "first_run_policy": "After first A/B this is an observed selection set; a changed candidate needs challenge-v2 for a new independent gate.",
}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def decoy_ids() -> list[str]:
    queries = [json.loads(line) for line in (ROOT / "queries.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    ids = [row["query_id"] for row in queries if "cross_domain_decoy" in row["mechanism_tags"]]
    if len(queries) != 24 or len(ids) != 6 or len(set(ids)) != 6:
        raise ValueError("challenge decoy query set changed")
    return ids


def expected() -> dict:
    return {
        **PROTOCOL,
        "cross_domain_decoy_query_ids": decoy_ids(),
        "locked_sha256": {path.as_posix(): sha256(path) for path in LOCKED_FILES},
    }


def build() -> None:
    check_challenge()
    if GATE.exists():
        raise ValueError(f"gate already frozen: {GATE}")
    GATE.write_text(json.dumps(expected(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def check() -> None:
    check_challenge()
    actual = json.loads(GATE.read_text(encoding="utf-8"))
    if actual != expected():
        raise ValueError("gate protocol, challenge, artifact, or code changed")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("build", "check"))
    args = parser.parse_args()
    if args.action == "build":
        build()
    check()
    print("PASS: challenge A/B gate and locked input hashes verified; no retrieval run")
