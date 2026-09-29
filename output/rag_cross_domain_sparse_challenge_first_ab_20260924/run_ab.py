"""One-shot, pre-registered challenge A/B; never changes retrieval defaults."""

import argparse
import hashlib
import json
from pathlib import Path

from scripts.freeze_rag_cross_domain_sparse_challenge import check as check_challenge
from scripts.validate_rag_cross_domain_sparse_gate import check as check_gate


BENCH = Path("benchmarks/rag_cross_domain_sparse_challenge_v1")
INDEX = Path("artifacts/rag_round3/production_indexes")
OUTPUT = Path("artifacts/rag_cross_domain_sparse_challenge_first_ab_20260924")
SNAPSHOT = Path("artifacts/rag_holdout_candidates_20260921/protected_hashes_after.json")


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save(name: str, value: object) -> None:
    (OUTPUT / name).write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def preflight() -> dict:
    if OUTPUT.exists():
        raise ValueError(f"one-shot output directory already exists: {OUTPUT}")
    check_challenge()
    check_gate()
    protected = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    index_files = {name: expected for name, expected in protected.items() if name.startswith(INDEX.as_posix() + "/")}
    faiss_files = {name for name in index_files if name.endswith("/index.faiss")}
    if len(index_files) != 10 or faiss_files != {
        (INDEX / domain / "index.faiss").as_posix() for domain in ("apple_support", "agent_engineering")
    }:
        raise ValueError("protected production index snapshot is incomplete")
    changed = [name for name, expected in index_files.items() if digest(Path(name)) != expected]
    if changed:
        raise ValueError(f"production index snapshot drift: {changed}")
    return {
        "challenge_check": "PASS", "gate_check": "PASS",
        "protected_index_files": len(index_files),
        "protected_snapshot_sha256": digest(SNAPSHOT),
        "gate_sha256": digest(BENCH / "ab_gate.json"),
        "index_file_sha256": index_files,
    }


def grade2_losses(rows_a: list[dict], rows_b: list[dict], groups: dict[str, list[dict]]) -> list[dict]:
    lost = []
    for a, b in zip(rows_a, rows_b, strict=True):
        if a["query_id"] != b["query_id"]:
            raise ValueError("A/B query order changed")
        ids_a = set(a["rankings"]["hybrid_rerank"])
        ids_b = set(b["rankings"]["hybrid_rerank"])
        for group in groups[a["query_id"]]:
            members = set(group["member_chunk_ids"])
            if group["relevance"] == 2 and ids_a & members and not ids_b & members:
                lost.append({"query_id": a["query_id"], "group_id": group["group_id"]})
    return lost


def gate_report(a: dict, b: dict, groups: dict[str, list[dict]], gate: dict) -> dict:
    rows_a, rows_b = a["per_query"], b["per_query"]
    if len(rows_a) != len(rows_b) or len(rows_a) != 24:
        raise ValueError("A/B query count differs from gate")
    ids = gate["cross_domain_decoy_query_ids"]
    decoy_a = [row["variants"]["bm25"]["wrong_domain_rate@10"] for row in rows_a if row["query_id"] in ids]
    decoy_b = [row["variants"]["bm25"]["wrong_domain_rate@10"] for row in rows_b if row["query_id"] in ids]
    if len(decoy_a) != len(decoy_b) or len(decoy_a) != 6:
        raise ValueError("decoy query count changed")
    checks = {}

    def add(name: str, left: float, right: float, rule: str, passed: bool) -> None:
        checks[name] = {"A": left, "B": right, "delta_B_minus_A": right - left, "rule": rule, "status": "PASS" if passed else "FAIL"}

    x, y = sum(decoy_a) / 6, sum(decoy_b) / 6
    floor = x < 0.10
    add("sparse_mechanism.bm25_decoy_wrong_domain_rate@10", x, y,
        "B <= A" if floor else "B <= A - 0.10", y <= (x if floor else x - 0.10))
    tests = (
        ("sparse_mechanism.bm25_recall@10", "bm25", "recall@10", -0.02, "gte"),
        ("rrf.hybrid_rrf_recall@10", "hybrid_rrf", "recall@10", 0, "gte"),
        ("rrf.hybrid_rrf_wrong_domain_rate@10", "hybrid_rrf", "wrong_domain_rate@10", 0, "lte"),
        ("final_ce.hybrid_rerank_recall@10", "hybrid_rerank", "recall@10", 0, "gte"),
        ("final_ce.hybrid_rerank_wrong_domain_rate@10", "hybrid_rerank", "wrong_domain_rate@10", 0, "lte"),
        ("final_ce.hybrid_rerank_mrr@10", "hybrid_rerank", "mrr@10", -0.01, "gte"),
        ("final_ce.hybrid_rerank_ndcg@10", "hybrid_rerank", "ndcg@10", -0.01, "gte"),
    )
    for name, variant, metric, allowance, direction in tests:
        x, y = a["overall"][variant][metric], b["overall"][variant][metric]
        rule = (f"B >= A {allowance:+g}" if allowance else "B >= A") if direction == "gte" else "B <= A"
        add(name, x, y, rule, y >= x + allowance if direction == "gte" else y <= x)
    for domain in ("apple_support", "agent_engineering"):
        x = a["by_domain"][domain]["hybrid_rerank"]["recall@10"]
        y = b["by_domain"][domain]["hybrid_rerank"]["recall@10"]
        add(f"domain_safety.{domain}_hybrid_rerank_recall@10", x, y, "B >= A - 0.02", y >= x - 0.02)
    lost = grade2_losses(rows_a, rows_b, groups)
    add("final_ce.newly_lost_grade_2_fact_groups@ce10", 0, len(lost), "count == 0", not lost)
    required = {f"{section}.{name}" for section, values in gate["gates"].items() for name in values}
    if set(checks) != required:
        raise ValueError(f"gate implementation differs from preregistration: {sorted(required ^ set(checks))}")
    all_pass = all(item["status"] == "PASS" for item in checks.values())
    return {
        "gate_sha256": digest(BENCH / "ab_gate.json"), "checks": checks,
        "newly_lost_grade_2_fact_groups": lost,
        "decision": gate["decision"]["pass" if all_pass else "fail"],
        "all_required_gates_pass": all_pass,
        "challenge_is_now_seen_selection_set": True,
    }


def run() -> None:
    from rag.embeddings import SentenceTransformerEmbeddingBackend
    from rag.evaluation.evaluator import evaluate_variants
    from rag.reranker import CrossEncoderReranker
    from rag.retriever import HybridRetriever
    from scripts.evaluate_rag_retrieval import load_queries, load_relevance_groups
    from scripts.validate_rag_models import validate

    preflight_info = preflight()
    model = validate(local_only=True)
    if not (model["status"] == "ready" and model["embedding_model"] == "BAAI/bge-m3"
            and model["embedding_dimension"] == 1024 and model["reranker_model"] == "BAAI/bge-reranker-v2-m3"
            and model["fake_embedding"] is False and model["fake_reranker"] is False):
        OUTPUT.mkdir(parents=True)
        save("preflight.json", preflight_info)
        save("model_validation.json", model)
        raise RuntimeError("real local models are not ready; A/B metrics were not generated")
    OUTPUT.mkdir(parents=True)
    save("preflight.json", preflight_info)
    save("model_validation.json", model)
    queries = load_queries(BENCH)
    groups = load_relevance_groups(BENCH / "qrel_groups.jsonl", queries=queries)
    backend = SentenceTransformerEmbeddingBackend()
    reranker = CrossEncoderReranker()
    reports = {}
    for arm, sparse_mode in (("A", "domain_local_v1"), ("B", "global_corpus_v1")):
        retriever = HybridRetriever(INDEX, embedding_backend=backend, reranker=reranker,
                                    allow_dry_run=False, sparse_mode=sparse_mode)
        reports[arm] = evaluate_variants(retriever, queries, relevance_groups_by_query=groups)
    rows_a, rows_b = reports["A"]["per_query"], reports["B"]["per_query"]
    if [row["query_id"] for row in rows_a] != [row.query_id for row in queries] or [row["query_id"] for row in rows_b] != [row.query_id for row in queries]:
        raise ValueError("A/B query order differs from frozen challenge")
    dense_equal = sum(a["rankings"]["dense"] == b["rankings"]["dense"] for a, b in zip(rows_a, rows_b, strict=True))
    if dense_equal != 24:
        raise ValueError(f"A/B dense mismatch on {24 - dense_equal} queries")
    for arm, report in reports.items():
        save(f"arm_{arm}_metrics.json", report["overall"])
        save(f"arm_{arm}_metrics_by_domain.json", report["by_domain"])
        save(f"arm_{arm}_metrics_per_query.json", report["per_query"])
    gate = json.loads((BENCH / "ab_gate.json").read_text(encoding="utf-8"))
    result = gate_report(reports["A"], reports["B"], groups, gate)
    result["dense_parity_queries"] = dense_equal
    save("gate_report.json", result)
    lines = ["# First challenge A/B", "", f"Decision: **{result['decision']}**", "", "A=domain_local_v1; B=global_corpus_v1. Challenge v1 is now a seen selection set.", "", "| Gate | A | B | Delta | Rule | Status |", "|---|---:|---:|---:|---|---|"]
    for name, item in result["checks"].items():
        lines.append(f"| {name} | {item['A']:.6f} | {item['B']:.6f} | {item['delta_B_minus_A']:+.6f} | {item['rule']} | {item['status']} |")
    (OUTPUT / "comparison.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({"status": "PASS", "decision": result["decision"], "output": str(OUTPUT)}, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--preflight-only", action="store_true")
    args = parser.parse_args()
    if args.preflight_only:
        info = preflight()
        print(json.dumps({"status": "PASS", "protected_index_files": info["protected_index_files"], "gate_sha256": info["gate_sha256"]}))
    else:
        run()
