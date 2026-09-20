"""Validate and manifest the manually curated Round 3 benchmark package."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


DOMAINS = ("apple_support", "agent_engineering")
KINDS = ("semantic", "lexical", "confusing")


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _load_chunks(index_root: Path, domain: str) -> dict[str, dict[str, Any]]:
    path = index_root / domain / "chunks.jsonl"
    rows = _read_jsonl(path)
    chunks = {str(row["chunk_id"]): row for row in rows}
    if not chunks:
        raise ValueError(f"no chunks for {domain}: {path}")
    return chunks


def _validate(
    queries: list[dict[str, Any]],
    qrels: list[dict[str, Any]],
    chunks_by_domain: dict[str, dict[str, dict[str, Any]]],
) -> dict[str, Any]:
    if len(queries) != 60 or len({row.get("query_id") for row in queries}) != 60:
        raise ValueError("benchmark must contain 60 unique queries")
    if {row.get("domain") for row in queries} != set(DOMAINS):
        raise ValueError("benchmark domains must be exactly apple_support and agent_engineering")
    domain_counts = Counter(str(row["domain"]) for row in queries)
    if any(domain_counts[domain] != 30 for domain in DOMAINS):
        raise ValueError(f"domain counts must be 30/30: {domain_counts}")
    kind_counts = {
        domain: Counter(str(row["kind"]) for row in queries if row["domain"] == domain)
        for domain in DOMAINS
    }
    if any(
        set(counts) != set(KINDS) or any(counts[kind] != 10 for kind in KINDS)
        for counts in kind_counts.values()
    ):
        raise ValueError(f"each domain needs 10 semantic/lexical/confusing queries: {kind_counts}")
    if any(not str(row.get("query", "")).strip() or len(str(row["query"])) < 12 for row in queries):
        raise ValueError("queries must be non-empty natural-language questions")
    query_domains = {str(row["query_id"]): str(row["domain"]) for row in queries}
    by_query: dict[str, list[dict[str, Any]]] = defaultdict(list)
    seen_pairs: set[tuple[str, str]] = set()
    relevance_values: set[int] = set()
    for row in qrels:
        required = {"query_id", "chunk_id", "relevance", "domain", "source", "heading_path", "rationale"}
        if not required <= set(row):
            raise ValueError(f"qrel missing audit fields: {row}")
        query_id = str(row["query_id"])
        domain = str(row["domain"])
        pair = (query_id, str(row["chunk_id"]))
        if pair in seen_pairs:
            raise ValueError(f"duplicate qrel pair: {row}")
        seen_pairs.add(pair)
        if domain not in DOMAINS or str(row["chunk_id"]) not in chunks_by_domain[domain]:
            raise ValueError(f"qrel chunk does not exist in its domain: {row}")
        if query_id not in query_domains or query_domains[query_id] != domain:
            raise ValueError(f"qrel domain does not match its query: {row}")
        chunk = chunks_by_domain[domain][str(row["chunk_id"])]
        if str(row["source"]) != str(chunk.get("source", "")):
            raise ValueError(f"qrel source does not match chunk metadata: {row}")
        if list(row["heading_path"]) != list(chunk.get("heading_path") or []):
            raise ValueError(f"qrel heading_path does not match chunk metadata: {row}")
        if not str(row["rationale"]).strip():
            raise ValueError(f"qrel rationale must be non-empty: {row}")
        relevance = int(row["relevance"])
        if relevance not in (1, 2):
            raise ValueError(f"qrels must use relevance 1/2: {row}")
        relevance_values.add(relevance)
        by_query[query_id].append(row)
    query_ids = {str(row["query_id"]) for row in queries}
    if set(by_query) != query_ids or any(
        not any(int(item["relevance"]) >= 1 for item in rows) for rows in by_query.values()
    ):
        raise ValueError("every query must have at least one relevant qrel")
    if relevance_values != {1, 2}:
        raise ValueError("benchmark must contain both relevance=2 and supporting relevance=1")
    return {
        "query_count": len(queries),
        "qrel_count": len(qrels),
        "domain_counts": dict(sorted(domain_counts.items())),
        "query_kinds": {domain: dict(sorted(kind_counts[domain].items())) for domain in DOMAINS},
        "relevance_distribution": dict(sorted(Counter(str(row["relevance"]) for row in qrels).items())),
    }


def build(index_root: Path, output_root: Path) -> dict[str, Any]:
    """Validate checked-in query/qrel data and write a reproducibility manifest."""

    queries = _read_jsonl(output_root / "queries.jsonl")
    qrels = _read_jsonl(output_root / "qrels.jsonl")
    chunks_by_domain = {domain: _load_chunks(index_root, domain) for domain in DOMAINS}
    summary = _validate(queries, qrels, chunks_by_domain)
    manifest = {
        "benchmark_version": "rag-round3-v3-curated",
        "chunking_version": "rag-round1",
        "index_root": str(index_root),
        "domains": summary["domain_counts"],
        "query_count": summary["query_count"],
        "qrel_count": summary["qrel_count"],
        "relevance_scale": {"2": "direct answer", "1": "supporting material", "0": "not included"},
        "query_kinds": summary["query_kinds"],
        "relevance_distribution": summary["relevance_distribution"],
        "authoring": "manual-curated; query text is maintained in queries.jsonl",
        "source_sha256": {
            domain: hashlib.sha256((index_root / domain / "chunks.jsonl").read_bytes()).hexdigest()
            for domain in DOMAINS
        },
    }
    (output_root / "benchmark_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--index-root", type=Path, default=Path("artifacts/rag_round1/indexes"))
    parser.add_argument("--output-root", type=Path, default=Path("benchmarks/rag"))
    args = parser.parse_args()
    print(json.dumps(build(args.index_root, args.output_root), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
