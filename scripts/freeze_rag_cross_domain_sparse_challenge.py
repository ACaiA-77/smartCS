"""Freeze GPT-reviewed source-first challenge candidates without running retrieval."""

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path

from output.rag_cross_domain_sparse_challenge_20260923.audit_candidates import main as audit_candidates
from scripts.evaluate_rag_retrieval import load_queries, load_relevance_groups, validate_benchmark_manifest


CANDIDATES = Path("artifacts/rag_cross_domain_sparse_challenge_candidates_20260923")
GOLD = Path("benchmarks/rag_cross_domain_sparse_challenge_v1")
INDEX = Path("artifacts/rag_round3/production_indexes")
FILES = {"queries.jsonl": "proposed_queries.jsonl", "qrels.jsonl": "proposed_qrels.jsonl", "qrel_groups.jsonl": "qrel_groups.jsonl"}


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def build() -> None:
    audit_candidates()
    assert (CANDIDATES / "source_completeness_audit.md").is_file()
    assert not GOLD.exists(), f"frozen benchmark already exists: {GOLD}"
    GOLD.mkdir(parents=True)
    for target, source in FILES.items():
        rows = load(CANDIDATES / source)
        for row in rows:
            row.pop("status", None)
        (GOLD / target).write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
    queries = load(GOLD / "queries.jsonl")
    domain_counts = Counter(query["domain"] for query in queries)
    kind_counts = {domain: dict(Counter(query["kind"] for query in queries if query["domain"] == domain)) for domain in domain_counts}
    tag_counts = Counter(tag for query in queries for tag in query["mechanism_tags"])
    manifest = {
        "benchmark_version": "rag-cross-domain-sparse-challenge-v1",
        "split": "challenge",
        "purpose": "engineering_model_selection",
        "blindness": "not_unseen_holdout",
        "known_dev_history_exposure": True,
        "scoring_mode": "fact_group_v1",
        "chunking_version": "structure-context-v1",
        "index_root": INDEX.as_posix(),
        "query_count": 24,
        "qrel_count": 49,
        "raw_qrel_count": 49,
        "fact_group_count": 32,
        "alias_count": 17,
        "domains": dict(domain_counts),
        "query_kinds": kind_counts,
        "mechanism_tags": dict(tag_counts),
        "qrel_completeness": "source_order_reviewed_not_corpus_exhaustive",
        "review": "source-first authored engineering challenge; semantic review completed under user-delegated selection; not an unseen Holdout",
        "review_result": "Smartcs-new readonly REVIEW=PASS iteration 2 on 2026-09-24",
        "source_sha256": {domain: digest(INDEX / domain / "chunks.jsonl") for domain in ("apple_support", "agent_engineering")},
        "file_sha256": {name: digest(GOLD / name) for name in FILES},
    }
    manifest["qrel_groups_sha256"] = manifest["file_sha256"]["qrel_groups.jsonl"]
    (GOLD / "benchmark_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def check() -> None:
    manifest = json.loads((GOLD / "benchmark_manifest.json").read_text(encoding="utf-8"))
    assert manifest["split"] == "challenge" and manifest["scoring_mode"] == "fact_group_v1"
    assert (manifest["query_count"], manifest["qrel_count"], manifest["fact_group_count"], manifest["alias_count"]) == (24, 49, 32, 17)
    assert manifest["domains"] == {"apple_support": 12, "agent_engineering": 12}
    assert all(counts == {"semantic": 4, "lexical": 4, "confusing": 4} for counts in manifest["query_kinds"].values())
    validate_benchmark_manifest(GOLD, INDEX)
    queries = load_queries(GOLD)
    groups = load_relevance_groups(GOLD / "qrel_groups.jsonl", queries=queries)
    assert len(queries) == 24 and sum(len(query.qrels) for query in queries) == 49
    assert sum(len(items) for items in groups.values()) == 32


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("build", "check"))
    args = parser.parse_args()
    if args.action == "build":
        build()
    check()
    print("PASS: frozen challenge source and file hashes verified")
