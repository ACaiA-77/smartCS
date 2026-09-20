"""Evaluate the four fixed Round 3 retrieval variants."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from ..fusion import reciprocal_rank_fusion
from ..models import RetrievalHit
from ..retriever import global_ranked_candidates
from .metrics import aggregate_metrics, evaluate_ranking
from .models import BenchmarkQuery


def _ids(hits: Iterable[RetrievalHit]) -> list[str]:
    return [RetrievalHit.from_value(hit).chunk_id for hit in hits]


def _rank_from_candidates(
    retriever: Any,
    query: str,
    dense: list[RetrievalHit],
    sparse: list[RetrievalHit],
) -> dict[str, list[RetrievalHit]]:
    fused = reciprocal_rank_fusion(dense, sparse, rrf_k=60, top_k=20)
    reranked = retriever.rerank(query, fused, top_k=10)
    return {
        "dense": [RetrievalHit.from_value(hit) for hit in dense[:10]],
        "bm25": [RetrievalHit.from_value(hit) for hit in sparse[:10]],
        "hybrid_rrf": [RetrievalHit.from_value(hit) for hit in fused[:10]],
        "hybrid_rerank": [RetrievalHit.from_value(hit) for hit in reranked[:10]],
    }


def _rank_variant_hits(
    retriever: Any, query: str, *, domains: str | None
) -> dict[str, list[RetrievalHit]]:
    """Return fixed-candidate rankings without query rewrite."""

    dense = global_ranked_candidates(
        retriever.dense_search(query, domains=domains, top_k=20),
        top_k=20,
        rank_field="dense_rank",
    )
    sparse = global_ranked_candidates(
        retriever.sparse_search(query, domains=domains, top_k=20),
        top_k=20,
        rank_field="sparse_rank",
    )
    return _rank_from_candidates(retriever, query, dense, sparse)


def rank_variants(retriever: Any, query: str, domain: str | None = None) -> dict[str, list[str]]:
    """Return fixed-candidate chunk IDs for the selected domain."""

    return {variant: _ids(hits) for variant, hits in _rank_variant_hits(retriever, query, domains=domain).items()}


def _wrong_domain_metrics(hits: list[RetrievalHit], domain: str) -> dict[str, float]:
    result: dict[str, float] = {}
    for cutoff in (1, 3, 5, 10):
        top = hits[:cutoff]
        result[f"wrong_domain_rate@{cutoff}"] = (
            sum(hit.domain != domain for hit in top) / len(top) if top else 0.0
        )
    return result


def evaluate_variants(retriever: Any, queries: Iterable[BenchmarkQuery]) -> dict[str, Any]:
    per_query: list[dict[str, Any]] = []
    for item in queries:
        dense = global_ranked_candidates(
            retriever.dense_search(item.query, domains=None, top_k=20),
            top_k=20,
            rank_field="dense_rank",
        )
        sparse = global_ranked_candidates(
            retriever.sparse_search(item.query, domains=None, top_k=20),
            top_k=20,
            rank_field="sparse_rank",
        )
        rankings = _rank_from_candidates(retriever, item.query, dense, sparse)
        row: dict[str, Any] = {
            "query_id": item.query_id,
            "query": item.query,
            "domain": item.domain,
            "kind": item.kind,
            "qrels": item.qrels,
            "rankings": {variant: _ids(hits) for variant, hits in rankings.items()},
            "variants": {},
        }
        for variant, hits in rankings.items():
            metrics = evaluate_ranking(_ids(hits), item.qrels)
            metrics.update(_wrong_domain_metrics(hits, item.domain))
            row["variants"][variant] = metrics
        per_query.append(row)
    variants = sorted({variant for row in per_query for variant in row["variants"]})
    overall = {
        variant: aggregate_metrics(
            row["variants"][variant] for row in per_query if variant in row["variants"]
        )
        for variant in variants
    }
    by_domain = {
        domain: {
            variant: aggregate_metrics(
                row["variants"][variant]
                for row in per_query
                if row["domain"] == domain and variant in row["variants"]
            )
            for variant in variants
        }
        for domain in sorted({row["domain"] for row in per_query})
    }
    return {"overall": overall, "by_domain": by_domain, "per_query": per_query}


__all__ = ["evaluate_variants", "rank_variants"]
