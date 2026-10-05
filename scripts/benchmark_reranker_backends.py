"""Phase 12 §2: compare reranker backends on identical batches.

Speed alone does not decide the backend — a faster reranker that reorders the
candidates is a different retriever, not a faster one. So this reports both:

  * **latency** per batch, per backend, on the real fused candidate sets;
  * **agreement** with the `cross_encoder` reference: Spearman rank correlation
    and top-1 / top-3 / full-order agreement, per query and pooled.

    python -m scripts.benchmark_reranker_backends
    python -m scripts.benchmark_reranker_backends --queries 60 --json out.json

Batches are built with `HybridRetriever`'s own dense/sparse/RRF stages, so the
inputs are exactly what production reranks; only the scoring backend differs.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

DEFAULT_BENCHMARK_ROOT = Path("benchmarks/rag")
DEFAULT_ARTIFACT_ROOT = Path("artifacts/rag_round3/production_indexes")


def _rank(values: Sequence[float]) -> list[float]:
    """Average ranks, so ties do not fabricate an ordering."""

    order = sorted(range(len(values)), key=lambda index: values[index])
    ranks = [0.0] * len(values)
    position = 0
    while position < len(order):
        end = position
        while end + 1 < len(order) and values[order[end + 1]] == values[order[position]]:
            end += 1
        average = (position + end) / 2.0 + 1.0
        for index in range(position, end + 1):
            ranks[order[index]] = average
        position = end + 1
    return ranks


def spearman(left: Sequence[float], right: Sequence[float]) -> float:
    """Spearman rho = Pearson correlation of the average ranks."""

    if len(left) != len(right):
        raise ValueError("spearman needs two equal-length samples")
    if len(left) < 2:
        return 1.0
    rank_left, rank_right = _rank(left), _rank(right)
    mean_left = statistics.fmean(rank_left)
    mean_right = statistics.fmean(rank_right)
    covariance = sum(
        (a - mean_left) * (b - mean_right) for a, b in zip(rank_left, rank_right)
    )
    spread_left = sum((a - mean_left) ** 2 for a in rank_left) ** 0.5
    spread_right = sum((b - mean_right) ** 2 for b in rank_right) ** 0.5
    if spread_left == 0.0 or spread_right == 0.0:
        # A constant score vector carries no ordering to agree or disagree with.
        return 1.0 if list(left) == list(right) else 0.0
    return covariance / (spread_left * spread_right)


def _percentile(values: list[float], percent: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = int(round((percent / 100.0) * len(ordered) + 0.5))
    return ordered[min(max(rank - 1, 0), len(ordered) - 1)]


def build_batches(queries: list[dict[str, Any]], *, top_k: int, pairs: int) -> list[dict[str, Any]]:
    """Fused candidate sets, built by the same stages production runs."""

    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    from memory.knowledge import KnowledgeMemory
    from rag.fusion import reciprocal_rank_fusion
    from rag.models import RetrievalHit
    from rag.retriever import global_ranked_candidates

    memory = KnowledgeMemory(index_path=os.getenv("FAISS_INDEX_PATH", "./vector_store/faiss_index"))
    retriever = memory.get_retriever()
    batches = []
    for row in queries:
        query = str(row["query"])
        dense = global_ranked_candidates(
            retriever.dense_search(query, domains=None, top_k=20), top_k=20, rank_field="dense_rank"
        )
        sparse = global_ranked_candidates(
            retriever.sparse_search(query, domains=None, top_k=20), top_k=20, rank_field="sparse_rank"
        )
        fused = reciprocal_rank_fusion(dense, sparse, rrf_k=60, top_k=pairs)
        batches.append(
            {
                "query_id": row.get("query_id"),
                "query": query,
                "candidates": [RetrievalHit.from_value(hit) for hit in fused],
            }
        )
    return batches


def measure(backend, batches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    import copy

    rows = []
    for batch in batches:
        # Deep copies: `rerank` stamps rank/score onto the hit objects in place,
        # and `RetrievalHit.from_value` returns the *same* object for a hit. The
        # second backend must not inherit the first one's scores.
        candidates = copy.deepcopy(batch["candidates"])
        started = time.perf_counter()
        # Score every candidate, not just top_k — agreement over 3 of 9 pairs
        # would be a much weaker claim than it looks.
        ranked = backend.rerank(batch["query"], candidates, top_k=len(candidates))
        elapsed = (time.perf_counter() - started) * 1000.0
        rows.append(
            {
                "query_id": batch["query_id"],
                "ms": elapsed,
                "scored": {hit.chunk_id: float(hit.rerank_score) for hit in ranked},
                "order": [hit.chunk_id for hit in ranked],
            }
        )
    return rows


def summarize(reference: list[dict[str, Any]], candidate: list[dict[str, Any]]) -> dict[str, Any]:
    latencies = [row["ms"] for row in candidate]
    per_query = []
    for ref, cand in zip(reference, candidate):
        shared = [chunk_id for chunk_id in ref["scored"] if chunk_id in cand["scored"]]
        rho = spearman(
            [ref["scored"][chunk_id] for chunk_id in shared],
            [cand["scored"][chunk_id] for chunk_id in shared],
        )
        per_query.append(
            {
                "query_id": ref["query_id"],
                "spearman": round(rho, 6),
                "top1_match": ref["order"][:1] == cand["order"][:1],
                "top3_match": ref["order"][:3] == cand["order"][:3],
                "order_match": ref["order"] == cand["order"],
                "max_abs_score_delta": round(
                    max(abs(ref["scored"][c] - cand["scored"][c]) for c in shared), 8
                ),
            }
        )
    rhos = [row["spearman"] for row in per_query]
    return {
        "latency_ms": {
            "count": len(latencies),
            "p50": round(_percentile(latencies, 50), 1),
            "p95": round(_percentile(latencies, 95), 1),
            "mean": round(statistics.fmean(latencies), 1),
            "total": round(sum(latencies), 1),
        },
        "agreement": {
            "spearman_min": round(min(rhos), 6),
            "spearman_mean": round(statistics.fmean(rhos), 6),
            "spearman_above_0_95": sum(1 for rho in rhos if rho > 0.95),
            "queries": len(rhos),
            "top1_match_rate": round(sum(r["top1_match"] for r in per_query) / len(per_query), 4),
            "top3_match_rate": round(sum(r["top3_match"] for r in per_query) / len(per_query), 4),
            "order_match_rate": round(sum(r["order_match"] for r in per_query) / len(per_query), 4),
            "max_abs_score_delta": round(max(r["max_abs_score_delta"] for r in per_query), 8),
        },
        "per_query": per_query,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--benchmark-root", type=Path, default=DEFAULT_BENCHMARK_ROOT)
    parser.add_argument("--artifact-root", type=Path, default=DEFAULT_ARTIFACT_ROOT)
    parser.add_argument("--queries", type=int, default=None)
    parser.add_argument("--pairs", type=int, default=9, help="candidates per rerank batch (production shape is 9)")
    parser.add_argument("--top-k", type=int, default=3)
    parser.add_argument("--max-chars", type=int, default=None, help="default: each backend's own env/default")
    parser.add_argument("--json", type=Path, default=None)
    args = parser.parse_args(argv)

    os.environ.setdefault("RAG_INDEX_ROOT", str(args.artifact_root))
    queries = [
        json.loads(line)
        for line in (args.benchmark_root / "queries.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ][: args.queries]

    from rag.reranker import CrossEncoderReranker, OnnxReranker

    print(f"building {len(queries)} real rerank batches (pairs={args.pairs})", file=sys.stderr)
    batches = build_batches(queries, top_k=args.top_k, pairs=args.pairs)

    backends: dict[str, Any] = {"cross_encoder": CrossEncoderReranker(max_chars=args.max_chars)}
    try:
        backends["onnx_int8"] = OnnxReranker(max_chars=args.max_chars)
    except (FileNotFoundError, ImportError) as error:
        print(f"onnx_int8 unavailable: {error}", file=sys.stderr)

    results: dict[str, Any] = {}
    reference_rows = None
    for name, backend in backends.items():
        print(f"measuring {name} ({type(backend).__name__}, max_chars={backend.max_chars})", file=sys.stderr)
        import copy

        backend.rerank(  # warmup
            batches[0]["query"],
            copy.deepcopy(batches[0]["candidates"]),
            top_k=len(batches[0]["candidates"]),
        )
        rows = measure(backend, batches)
        if reference_rows is None:
            reference_rows = rows
            results[name] = {
                "backend": type(backend).__name__,
                "max_chars": backend.max_chars,
                "latency_ms": {
                    "count": len(rows),
                    "p50": round(_percentile([r["ms"] for r in rows], 50), 1),
                    "p95": round(_percentile([r["ms"] for r in rows], 95), 1),
                    "mean": round(statistics.fmean([r["ms"] for r in rows]), 1),
                    "total": round(sum(r["ms"] for r in rows), 1),
                },
            }
        else:
            results[name] = {"backend": type(backend).__name__, "max_chars": backend.max_chars, **summarize(reference_rows, rows)}

    report = {
        "queries": len(queries),
        "pairs": args.pairs,
        "top_k": args.top_k,
        "backends": results,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"wrote {args.json}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
