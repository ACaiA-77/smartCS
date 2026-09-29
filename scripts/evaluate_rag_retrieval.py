"""Run the fixed Round 3 benchmark without online query rewriting."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from rag.evaluation.evaluator import evaluate_variants
from rag.evaluation.metrics import index_relevance_groups
from rag.evaluation.models import BenchmarkQuery
from rag.evaluation.report import write_json
from rag.embeddings import SentenceTransformerEmbeddingBackend
from rag.reranker import CrossEncoderReranker
from rag.retriever import HybridRetriever
from scripts.validate_rag_models import validate


def validate_benchmark_manifest(benchmark_root: Path, artifact_root: Path) -> None:
    """Reject evaluation when the benchmark was authored for other chunks."""

    manifest_path = benchmark_root / "benchmark_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    expected = manifest.get("source_sha256")
    if not isinstance(expected, dict) or not expected:
        raise ValueError("benchmark manifest source_sha256 is missing")
    file_hashes = manifest.get("file_sha256")
    if file_hashes is not None:
        if not isinstance(file_hashes, dict) or not file_hashes:
            raise ValueError("benchmark manifest file_sha256 is invalid")
        for name, expected_hash in sorted(file_hashes.items()):
            if not isinstance(name, str) or Path(name).name != name or name in (".", ".."):
                raise ValueError(f"benchmark manifest file name is invalid: {name}")
            path = benchmark_root / name
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != str(expected_hash):
                raise ValueError(f"benchmark manifest file hash mismatch for {name}")
    for domain, expected_hash in sorted(expected.items()):
        chunks_path = artifact_root / str(domain) / "chunks.jsonl"
        if not chunks_path.is_file():
            raise ValueError(f"benchmark manifest source hash cannot be checked: {chunks_path}")
        actual_hash = hashlib.sha256(chunks_path.read_bytes()).hexdigest()
        if actual_hash != str(expected_hash):
            raise ValueError(
                f"benchmark manifest source hash mismatch for {domain}: "
                f"expected {expected_hash}, got {actual_hash}"
            )


def load_queries(root: Path) -> list[BenchmarkQuery]:
    queries = [json.loads(line) for line in (root / "queries.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    qrels: dict[str, dict[str, int]] = {}
    for line in (root / "qrels.jsonl").read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        qrels.setdefault(str(row["query_id"]), {})[str(row["chunk_id"])] = int(row["relevance"])
    return [
        BenchmarkQuery(str(row["query_id"]), str(row["query"]), str(row["domain"]), str(row["kind"]), qrels.get(str(row["query_id"]), {}))
        for row in queries
    ]


def load_relevance_groups(
    path: Path, *, queries: list[BenchmarkQuery]
) -> dict[str, list[dict[str, Any]]]:
    """Load query-local fact groups, checking complete qrel coverage and regrades."""

    by_query: dict[str, list[dict[str, Any]]] = {}
    query_by_id = {query.query_id: query for query in queries}
    if len(query_by_id) != len(queries):
        raise ValueError("duplicate query_id in benchmark queries")
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if not isinstance(row, dict):
            raise ValueError("relevance group must be an object")
        query_id = row.get("query_id")
        if query_id not in query_by_id:
            raise ValueError(f"unknown query_id in relevance groups: {query_id}")
        by_query.setdefault(query_id, []).append(row)
    if set(by_query) != set(query_by_id):
        raise ValueError(f"missing relevance groups for: {sorted(set(query_by_id) - set(by_query))}")
    for query_id, groups in by_query.items():
        member_to_group, _ = index_relevance_groups(groups)
        raw_qrels = query_by_id[query_id].qrels
        if set(member_to_group) != set(raw_qrels):
            raise ValueError(f"relevance group members do not match qrels for {query_id}")
        for group in groups:
            for member in group["members"]:
                raw_grade = raw_qrels[member["chunk_id"]]
                if member["relevance"] != raw_grade and (
                    member.get("original_relevance") != raw_grade
                    or not member.get("adjudication_reason")
                ):
                    raise ValueError(f"unexplained relevance change for {query_id}: {member['chunk_id']}")
    return by_query


def run(
    *, benchmark_root: Path, artifact_root: Path, output_root: Path,
    qrel_groups_path: Path | None = None,
) -> dict[str, Any]:
    validate_benchmark_manifest(benchmark_root, artifact_root)
    manifest = json.loads((benchmark_root / "benchmark_manifest.json").read_text(encoding="utf-8"))
    if manifest.get("scoring_mode") == "fact_group_v1":
        if qrel_groups_path is None:
            raise ValueError("fact_group_v1 requires --qrel-groups")
        expected_hash = manifest.get("qrel_groups_sha256")
        if not isinstance(expected_hash, str) or not expected_hash:
            raise ValueError("fact_group_v1 requires qrel_groups_sha256")
        if hashlib.sha256(qrel_groups_path.read_bytes()).hexdigest() != expected_hash:
            raise ValueError("benchmark manifest group hash mismatch")
    queries = load_queries(benchmark_root) if qrel_groups_path is not None else None
    groups = load_relevance_groups(qrel_groups_path, queries=queries) if queries is not None else None
    model_validation = validate(local_only=True)
    write_json(output_root / "model_validation.json", model_validation)
    if model_validation["status"] != "ready":
        blocked = {
            "status": "BLOCKED",
            "reason": "real BGE-M3 and BGE reranker are not locally executable",
            "model_validation": model_validation,
            "variants": {name: None for name in ("dense", "bm25", "hybrid_rrf", "hybrid_rerank")},
        }
        write_json(output_root / "metrics.json", blocked)
        write_json(
            output_root / "metrics_by_domain.json",
            {"apple_support": None, "agent_engineering": None, "status": "BLOCKED"},
        )
        return blocked
    backend = SentenceTransformerEmbeddingBackend()
    retriever = HybridRetriever(
        artifact_root,
        embedding_backend=backend,
        reranker=CrossEncoderReranker(),
        allow_dry_run=False,
    )
    report = evaluate_variants(
        retriever,
        queries if queries is not None else load_queries(benchmark_root),
        relevance_groups_by_query=groups,
    )
    write_json(output_root / "metrics.json", report["overall"])
    write_json(output_root / "metrics_by_domain.json", report["by_domain"])
    write_json(output_root / "metrics_per_query.json", report["per_query"])
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--benchmark-root", type=Path, default=Path("benchmarks/rag"))
    parser.add_argument("--artifact-root", type=Path, default=Path("artifacts/rag_round3/production_indexes"))
    parser.add_argument("--output-root", type=Path, default=Path("artifacts/rag_round3"))
    parser.add_argument("--qrel-groups", type=Path, help="explicitly enable fact_group_v1 scoring")
    args = parser.parse_args()
    result = run(
        benchmark_root=args.benchmark_root, artifact_root=args.artifact_root,
        output_root=args.output_root, qrel_groups_path=args.qrel_groups,
    )
    print(json.dumps({"status": result.get("status", "ready"), "output_root": str(args.output_root)}, ensure_ascii=False))
    return 0 if result.get("status", "ready") != "BLOCKED" else 2


if __name__ == "__main__":
    raise SystemExit(main())
