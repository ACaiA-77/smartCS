"""Rank based fusion for dense and sparse retrieval lists."""

from __future__ import annotations

from .models import RetrievalHit


def reciprocal_rank_fusion(
    dense_hits: list[RetrievalHit | dict],
    sparse_hits: list[RetrievalHit | dict],
    *,
    rrf_k: int = 60,
    top_k: int | None = None,
) -> list[RetrievalHit]:
    if rrf_k < 1:
        raise ValueError("rrf_k must be positive")
    merged: dict[str, RetrievalHit] = {}
    for kind, hits in (("dense", dense_hits), ("sparse", sparse_hits)):
        for position, value in enumerate(hits, start=1):
            hit = RetrievalHit.from_value(value)
            if not hit.chunk_id:
                continue
            rank = int(hit.rank or position)
            existing = merged.get(hit.chunk_id)
            if existing is None:
                existing = RetrievalHit(
                    chunk_id=hit.chunk_id,
                    domain=hit.domain,
                    rank=0,
                    score=0.0,
                    source=hit.source,
                    content=hit.content,
                    retrieval_text=hit.retrieval_text or hit.content,
                    heading_path=list(hit.heading_path),
                    metadata=dict(hit.metadata),
                )
                merged[hit.chunk_id] = existing
            if kind == "dense":
                existing.dense_rank = rank
                existing.dense_score = hit.score
            else:
                existing.sparse_rank = rank
                existing.sparse_score = hit.score
            if not existing.content and hit.content:
                existing.content = hit.content
            if not existing.retrieval_text and hit.retrieval_text:
                existing.retrieval_text = hit.retrieval_text
            if not existing.source and hit.source:
                existing.source = hit.source
            if not existing.domain and hit.domain:
                existing.domain = hit.domain
            existing.rrf_score = (existing.rrf_score or 0.0) + 1.0 / (rrf_k + rank)
    ordered = sorted(
        merged.values(),
        key=lambda item: (
            -(item.rrf_score or 0.0),
            item.dense_rank or 10**9,
            item.sparse_rank or 10**9,
            item.chunk_id,
        ),
    )
    for rank, hit in enumerate(ordered, start=1):
        hit.rank = rank
        hit.score = float(hit.rrf_score or 0.0)
    return ordered if top_k is None else ordered[: max(0, top_k)]


class RRFFusion:
    def __init__(self, rrf_k: int = 60) -> None:
        self.rrf_k = rrf_k

    def fuse(self, dense_hits, sparse_hits, top_k: int | None = None):
        return reciprocal_rank_fusion(
            dense_hits, sparse_hits, rrf_k=self.rrf_k, top_k=top_k
        )


__all__ = ["RRFFusion", "reciprocal_rank_fusion"]
