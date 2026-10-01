"""Compare frozen fallback BM25 against jieba using the same gold benchmark.

The default three-variant diagnostic omits reranking. --include-rerank performs
real Cross-Encoder evaluation, journaling each query for safe --resume.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path
from typing import Any

from rag.embeddings import SentenceTransformerEmbeddingBackend
from rag.evaluation.evaluator import _wrong_domain_metrics
from rag.evaluation.metrics import aggregate_metrics, evaluate_grouped_ranking, evaluate_ranking
from rag.fusion import reciprocal_rank_fusion
from rag.models import RetrievalHit
from rag.reranker import CrossEncoderReranker, RERANKER_MODEL
from rag.retriever import HybridRetriever, global_ranked_candidates
from scripts.evaluate_rag_retrieval import (
    load_queries,
    load_relevance_groups,
    validate_benchmark_manifest,
)
from scripts.validate_rag_models import validate

DOMAINS = ("apple_support", "agent_engineering")


class _RerankingDisabled:
    """Reject accidental reranking without loading a model or returning fake scores."""

    def rerank(self, query: str, candidates: list[RetrievalHit], top_k: int = 3) -> list[RetrievalHit]:
        raise RuntimeError("reranking is disabled; pass --include-rerank for real scores")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _run_fingerprint(
    *, benchmark_root: Path, baseline_root: Path, candidate_root: Path,
    sparse_mode: str, qrel_groups_path: Path | None, jieba_version: str,
) -> dict[str, Any]:
    files: dict[str, str] = {}
    for label, root in (("baseline", baseline_root), ("candidate", candidate_root)):
        for domain in DOMAINS:
            for name in ("chunks.jsonl", "index.faiss", "manifest.json", "bm25_index.json"):
                files[f"{label}/{domain}/{name}"] = _sha256(root / domain / name)
        if sparse_mode == "global_corpus_v1":
            for name in ("bm25_index.json", "manifest.json"):
                files[f"{label}/global_sparse/{name}"] = _sha256(root / "global_sparse" / name)
    for name in ("benchmark_manifest.json", "queries.jsonl", "qrels.jsonl"):
        files[f"gold/{name}"] = _sha256(benchmark_root / name)
    if qrel_groups_path is not None:
        files["gold/qrel_groups.jsonl"] = _sha256(qrel_groups_path)
    return {
        "baseline_root": str(baseline_root.resolve()),
        "candidate_root": str(candidate_root.resolve()),
        "benchmark_root": str(benchmark_root.resolve()),
        "sparse_mode": sparse_mode,
        "scoring_mode": "fact_group_v1" if qrel_groups_path else "chunk_id",
        "embedding_model": "BAAI/bge-m3",
        "reranker_model": RERANKER_MODEL,
        "jieba_version": jieba_version,
        "files_sha256": files,
    }


def run(
    *, benchmark_root: Path, baseline_root: Path, candidate_root: Path,
    output_root: Path, sparse_mode: str = "domain_local_v1",
    qrel_groups_path: Path | None = None,
    include_rerank: bool = False, resume: bool = False,
) -> dict[str, Any]:
    import jieba

    if sparse_mode not in {"domain_local_v1", "global_corpus_v1"}:
        raise ValueError(f"unsupported sparse_mode: {sparse_mode}")
    if output_root.resolve() in {baseline_root.resolve(), candidate_root.resolve()}:
        raise ValueError("metrics output must not overwrite an index")
    if resume and not include_rerank:
        raise ValueError("--resume requires --include-rerank")
    if not resume and output_root.exists():
        raise FileExistsError(f"refusing to overwrite evaluation output: {output_root}")
    for root in (baseline_root, candidate_root):
        validate_benchmark_manifest(benchmark_root, root)
    for domain in DOMAINS:
        for filename in ("chunks.jsonl", "index.faiss"):
            if _sha256(baseline_root / domain / filename) != _sha256(candidate_root / domain / filename):
                raise ValueError(f"{domain}/{filename} changed: not an isolated sparse comparison")
    queries = load_queries(benchmark_root)
    print(f"{benchmark_root}: {len(queries)} queries, {sparse_mode}", flush=True)
    manifest = json.loads((benchmark_root / "benchmark_manifest.json").read_text(encoding="utf-8"))
    if manifest.get("scoring_mode") == "fact_group_v1":
        if qrel_groups_path is None:
            raise ValueError("fact_group_v1 requires qrel_groups_path")
        if _sha256(qrel_groups_path) != manifest.get("qrel_groups_sha256"):
            raise ValueError("qrel groups hash mismatch")
    groups = load_relevance_groups(qrel_groups_path, queries=queries) if qrel_groups_path else None
    production = json.loads((baseline_root / DOMAINS[0] / "manifest.json").read_text(encoding="utf-8"))["artifact_kind"] == "production"
    if include_rerank:
        if not production:
            raise ValueError("full rerank comparison requires real production artifacts")
        for domain in DOMAINS:
            manifest = json.loads((candidate_root / domain / "manifest.json").read_text(encoding="utf-8"))
            provenance = manifest.get("sparse_tokenizer_provenance") or {}
            if provenance.get("jieba_version") != jieba.__version__:
                raise ValueError(f"candidate {domain} tokenizer version mismatch")
        fingerprint = _run_fingerprint(
            benchmark_root=benchmark_root, baseline_root=baseline_root,
            candidate_root=candidate_root, sparse_mode=sparse_mode,
            qrel_groups_path=qrel_groups_path, jieba_version=jieba.__version__,
        )
        fingerprint_file = output_root / "run_manifest.json"
        if resume:
            if not fingerprint_file.is_file() or json.loads(fingerprint_file.read_text(encoding="utf-8")) != fingerprint:
                raise ValueError("--resume requires matching saved run_manifest.json and unchanged inputs")
        else:
            output_root.mkdir(parents=True)
            fingerprint_file.write_text(json.dumps(fingerprint, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        model_validation = validate(local_only=True)
        if model_validation.get("status") != "ready" or model_validation.get("fake_embedding") or model_validation.get("fake_reranker"):
            raise RuntimeError(f"real models are not executable: {model_validation.get('errors')}")
        (output_root / "model_validation.json").write_text(
            json.dumps(model_validation, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        (output_root / "progress").mkdir(exist_ok=True)
    backend = SentenceTransformerEmbeddingBackend()
    reranker = CrossEncoderReranker() if include_rerank else _RerankingDisabled()
    old = HybridRetriever(baseline_root, embedding_backend=backend, reranker=reranker, sparse_mode=sparse_mode)
    new = HybridRetriever(candidate_root, embedding_backend=backend, reranker=reranker, sparse_mode=sparse_mode)
    per_query: dict[str, list[dict[str, Any]]] = {"fallback": [], "jieba": []}
    installed_jieba = sys.modules["jieba"]
    for number, item in enumerate(queries, start=1):
        progress_file = output_root / "progress" / f"{number:03d}.json" if include_rerank else None
        if progress_file is not None and progress_file.is_file():
            saved = json.loads(progress_file.read_text(encoding="utf-8"))
            if saved.get("query_id") != item.query_id or set(saved.get("arms", {})) != {"fallback", "jieba"}:
                raise ValueError(f"invalid saved query checkpoint: {progress_file}")
            for arm in per_query:
                if "hybrid_rerank" not in saved["arms"][arm].get("variants", {}):
                    raise ValueError(f"missing rerank scores in checkpoint: {progress_file}")
                per_query[arm].append(saved["arms"][arm])
            print(f"{benchmark_root}: {number}/{len(queries)} reused", flush=True)
            continue
        started = time.perf_counter()
        dense = global_ranked_candidates(old.dense_search(item.query, domains=None, top_k=20),
                                         top_k=20, rank_field="dense_rank")
        for arm, retriever in (("fallback", old), ("jieba", new)):
            if arm == "fallback":
                sys.modules["jieba"] = None  # reproduce the old ImportError fallback in rag.build._segment_cjk
            try:
                sparse = global_ranked_candidates(
                    retriever.sparse_search(item.query, domains=None, top_k=20),
                    top_k=20, rank_field="sparse_rank",
                )
            finally:
                sys.modules["jieba"] = installed_jieba
            fused = reciprocal_rank_fusion(dense, sparse, rrf_k=60, top_k=20)
            rankings = {"dense": dense[:10], "bm25": sparse[:10], "hybrid_rrf": fused[:10]}
            if include_rerank:
                rankings["hybrid_rerank"] = retriever.rerank(item.query, fused, top_k=10)
            scores: dict[str, dict[str, float]] = {}
            for variant, hits in rankings.items():
                chunk_ids = [hit.chunk_id for hit in hits]
                scores[variant] = (
                    evaluate_grouped_ranking(chunk_ids, groups[item.query_id]) if groups is not None
                    else evaluate_ranking(chunk_ids, item.qrels)
                )
                scores[variant].update(_wrong_domain_metrics(hits, item.domain))
            per_query[arm].append({
                "query_id": item.query_id, "domain": item.domain, "kind": item.kind,
                "scoring_mode": "fact_group_v1" if groups is not None else "chunk_id",
                "rankings": {variant: [hit.chunk_id for hit in hits] for variant, hits in rankings.items()},
                "variants": scores,
            })
        if progress_file is not None:
            checkpoint = {"query_id": item.query_id, "arms": {arm: per_query[arm][-1] for arm in per_query}}
            pending = progress_file.with_suffix(".tmp")
            pending.write_text(json.dumps(checkpoint, ensure_ascii=False) + "\n", encoding="utf-8")
            pending.replace(progress_file)
        if include_rerank or number % 10 == 0 or number == len(queries):
            print(f"{benchmark_root}: {number}/{len(queries)} evaluated in {time.perf_counter()-started:.1f}s", flush=True)
    results: dict[str, Any] = {}
    for arm, rows in per_query.items():
        variants = ("dense", "bm25", "hybrid_rrf", "hybrid_rerank") if include_rerank else ("dense", "bm25", "hybrid_rrf")
        results[arm] = {
            "overall": {variant: aggregate_metrics(row["variants"][variant] for row in rows)
                        for variant in variants},
            "by_domain": {
                domain: {variant: aggregate_metrics(row["variants"][variant]
                                                    for row in rows if row["domain"] == domain)
                         for variant in variants}
                for domain in DOMAINS
            },
            "per_query": rows,
        }
    if any(a["rankings"]["dense"] != b["rankings"]["dense"] for a, b in
           zip(results["fallback"]["per_query"], results["jieba"]["per_query"], strict=True)):
        raise AssertionError("dense parity failed")
    result = {
        "benchmark": str(benchmark_root), "query_count": len(queries), "sparse_mode": sparse_mode,
        "jieba_version": jieba.__version__, "embedding_model": backend.model_name,
        "scoring_mode": "fact_group_v1" if groups is not None else "chunk_id",
        "reranker_status": "real_cross_encoder_complete" if include_rerank else "not_rerun (--include-rerank not requested; no rerank claim)",
        "old_index_root": str(baseline_root), "new_index_root": str(candidate_root),
        "baseline_query_tokenizer": "fallback", "candidate_query_tokenizer": "jieba",
        "arms": results,
    }
    output_root.mkdir(parents=True, exist_ok=True)
    report_path = output_root / "comparison.json"
    pending_report = report_path.with_suffix(".tmp")
    pending_report.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    pending_report.replace(report_path)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--benchmark-root", type=Path, default=Path("benchmarks/rag"))
    parser.add_argument("--baseline-root", type=Path, default=Path("artifacts/rag_round3/production_indexes"))
    parser.add_argument("--candidate-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--sparse-mode", choices=("domain_local_v1", "global_corpus_v1"), default="domain_local_v1")
    parser.add_argument("--qrel-groups", type=Path)
    parser.add_argument("--include-rerank", action="store_true", help="measure the real Cross-Encoder on all queries")
    parser.add_argument("--resume", action="store_true", help="continue the same rerank run from verified per-query checkpoints")
    args = parser.parse_args()
    report = run(benchmark_root=args.benchmark_root, baseline_root=args.baseline_root,
                 candidate_root=args.candidate_root, output_root=args.output_root,
                 sparse_mode=args.sparse_mode, qrel_groups_path=args.qrel_groups,
                 include_rerank=args.include_rerank, resume=args.resume)
    print(json.dumps({"query_count": report["query_count"], "output": str(args.output_root)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
