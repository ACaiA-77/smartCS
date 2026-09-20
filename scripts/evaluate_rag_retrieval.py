"""Run the fixed Round 3 benchmark without online query rewriting."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from rag.evaluation.evaluator import evaluate_variants
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


def run(*, benchmark_root: Path, artifact_root: Path, output_root: Path) -> dict[str, Any]:
    validate_benchmark_manifest(benchmark_root, artifact_root)
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
    report = evaluate_variants(retriever, load_queries(benchmark_root))
    write_json(output_root / "metrics.json", report["overall"])
    write_json(output_root / "metrics_by_domain.json", report["by_domain"])
    write_json(output_root / "metrics_per_query.json", report["per_query"])
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--benchmark-root", type=Path, default=Path("benchmarks/rag"))
    parser.add_argument("--artifact-root", type=Path, default=Path("artifacts/rag_round3/production_indexes"))
    parser.add_argument("--output-root", type=Path, default=Path("artifacts/rag_round3"))
    args = parser.parse_args()
    result = run(benchmark_root=args.benchmark_root, artifact_root=args.artifact_root, output_root=args.output_root)
    print(json.dumps({"status": result.get("status", "ready"), "output_root": str(args.output_root)}, ensure_ascii=False))
    return 0 if result.get("status", "ready") != "BLOCKED" else 2


if __name__ == "__main__":
    raise SystemExit(main())
