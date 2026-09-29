"""Freeze the approved 36-question Holdout without running retrieval."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path

from scripts.evaluate_rag_retrieval import (
    load_queries,
    load_relevance_groups,
    validate_benchmark_manifest,
)


CANDIDATES = Path("artifacts/rag_holdout_candidates_20260921")
GOLD = Path("benchmarks/rag_holdout_v1")
INDEX = Path("artifacts/rag_round3/production_indexes")
DOMAINS = ("agent_engineering", "apple_support")


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def encoded(rows_: list[dict]) -> bytes:
    return "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows_).encode("utf-8")


def validate(queries: list[dict], qrels: list[dict], groups: list[dict]) -> None:
    assert len(queries) == 36 and len(qrels) == 62 and len(groups) == 44
    query_ids = {row["query_id"] for row in queries}
    assert len(query_ids) == 36
    assert Counter((row["domain"], row["kind"]) for row in queries) == {
        (domain, kind): 6 for domain in DOMAINS for kind in ("semantic", "lexical", "confusing")
    }
    chunks = {
        row["chunk_id"]: row
        for domain in DOMAINS for row in rows(INDEX / domain / "chunks.jsonl")
    }
    qrel_by_key = {(row["query_id"], row["chunk_id"]): row for row in qrels}
    assert len(qrel_by_key) == len(qrels)
    for row in qrels:
        chunk = chunks[row["chunk_id"]]
        assert row["query_id"] in query_ids and row["relevance"] in (1, 2)
        assert all(row[key] == chunk[key] for key in ("domain", "source", "heading_path"))
    members: dict[tuple[str, str], dict] = {}
    for group in groups:
        assert group["query_id"] in query_ids and group["relevance"] in (1, 2)
        assert group["canonical_chunk_id"] in group["member_chunk_ids"]
        assert group["member_chunk_ids"] == [member["chunk_id"] for member in group["members"]]
        for member in group["members"]:
            key = (group["query_id"], member["chunk_id"])
            assert key not in members and member["relevance"] == group["relevance"]
            members[key] = member
    assert len(members) == 62 and set(members) == set(qrel_by_key)
    assert sum(len(group["members"]) - 1 for group in groups) == 18
    for key, row in qrel_by_key.items():
        member = members[key]
        if row["relevance"] != member["relevance"]:
            assert (key == ("holdout_agent_010", "4591f09d83fc04614edb56d8")
                    and row["relevance"] == member["original_relevance"] == 1
                    and member["relevance"] == 2 and member["adjudication_reason"])


def build() -> None:
    queries = rows(CANDIDATES / "proposed_queries.jsonl")
    qrels = rows(CANDIDATES / "proposed_qrels.jsonl")
    groups = rows(CANDIDATES / "qrel_equivalence_groups.jsonl")
    validate(queries, qrels, groups)
    grades = {
        (group["query_id"], member["chunk_id"]): member
        for group in groups for member in group["members"]
    }
    for row in queries:
        row.pop("status", None)
    for row in qrels:
        member = grades[row["query_id"], row["chunk_id"]]
        row.pop("status", None)
        if row["relevance"] != member["relevance"]:
            row["original_relevance"] = row["relevance"]
            row["adjudication_reason"] = member["adjudication_reason"]
            row["relevance"] = member["relevance"]
    GOLD.mkdir(parents=True, exist_ok=False)
    for name, data in (("queries", queries), ("qrels", qrels), ("qrel_groups", groups)):
        (GOLD / f"{name}.jsonl").write_bytes(encoded(data))
    manifest = {
        "benchmark_version": "rag-holdout-v1",
        "split": "holdout",
        "scoring_mode": "fact_group_v1",
        "chunking_version": "structure-context-v1",
        "index_root": str(INDEX).replace("\\", "/"),
        "query_count": 36,
        "raw_qrel_count": 62,
        "fact_group_count": 44,
        "alias_count": 18,
        "human_review": "36 question texts approved; 44 groups, grades and aliases accepted via user reply '合理' on 2026-09-23",
        "blindness": "partial",
        "known_dev_history_exposure": True,
        "qrel_completeness": "not established corpus-wide",
        "source_sha256": {domain: digest(INDEX / domain / "chunks.jsonl") for domain in DOMAINS},
        "file_sha256": {name: digest(GOLD / name) for name in ("queries.jsonl", "qrels.jsonl", "qrel_groups.jsonl")},
    }
    manifest["qrel_groups_sha256"] = manifest["file_sha256"]["qrel_groups.jsonl"]
    (GOLD / "benchmark_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def check() -> None:
    manifest = json.loads((GOLD / "benchmark_manifest.json").read_text(encoding="utf-8"))
    assert manifest["benchmark_version"] == "rag-holdout-v1"
    assert manifest["split"] == "holdout" and manifest["scoring_mode"] == "fact_group_v1"
    assert manifest["chunking_version"] == "structure-context-v1"
    assert [manifest[key] for key in ("query_count", "raw_qrel_count", "fact_group_count", "alias_count")] == [36, 62, 44, 18]
    for name, expected in manifest["file_sha256"].items():
        assert digest(GOLD / name) == expected
    assert manifest["qrel_groups_sha256"] == manifest["file_sha256"]["qrel_groups.jsonl"]
    validate_benchmark_manifest(GOLD, INDEX)
    queries = load_queries(GOLD)
    groups = load_relevance_groups(GOLD / "qrel_groups.jsonl", queries=queries)
    assert len(queries) == 36 and sum(len(query.qrels) for query in queries) == 62
    assert sum(len(items) for items in groups.values()) == 44
    assert next(row for row in rows(GOLD / "qrels.jsonl") if row["chunk_id"] == "4591f09d83fc04614edb56d8")["relevance"] == 2


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("build", "check"))
    action = parser.parse_args().action
    if action == "build":
        build()
    check()
    print("PASS: rag-holdout-v1 frozen data and source hashes verified")
