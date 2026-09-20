"""Bounded retrieval runtime used by the chat Agent."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from .models import RetrievalHit


@dataclass(frozen=True)
class RetrievalTrace:
    original_query: str
    rewritten_query: str
    rounds: int
    domains: tuple[str, ...]
    hits: list[RetrievalHit]

    def to_dict(self) -> dict[str, Any]:
        return {
            "original_query": self.original_query,
            "rewritten_query": self.rewritten_query,
            "rounds": self.rounds,
            "domains": list(self.domains),
            "hits": [hit.to_dict() for hit in self.hits],
        }


class RetrievalRuntime:
    """Run at most two bounded retrieval rounds, with one refinement fallback."""

    def __init__(self, retriever, *, max_retrieval_rounds: int = 2, quality_threshold: float = 0.0):
        if max_retrieval_rounds < 1:
            raise ValueError("max_retrieval_rounds must be positive")
        self.retriever = retriever
        self.max_retrieval_rounds = min(max_retrieval_rounds, 2)
        self.quality_threshold = quality_threshold

    def retrieve(
        self,
        original_query: str,
        rewritten_query: str,
        *,
        domains: Iterable[str] | str | None = None,
        top_k: int = 3,
        rerank: bool = True,
    ) -> RetrievalTrace:
        selected = (domains,) if isinstance(domains, str) else tuple(domains or ())
        current_query = str(rewritten_query or original_query).strip()
        hits: list[RetrievalHit] = []
        rounds = 0
        for rounds in range(1, self.max_retrieval_rounds + 1):
            hits = self.retriever.retrieve(
                current_query,
                domains=domains,
                top_k=top_k,
                rerank=rerank,
            )
            quality_scores = [
                float(hit.rrf_score if hit.rrf_score is not None else hit.score)
                for hit in hits
            ]
            if (
                current_query == str(original_query).strip()
                or any(score > self.quality_threshold for score in quality_scores)
            ):
                break
            # One refinement only: use the original wording while preserving
            # the caller's rewrite for the final answer and trace.
            current_query = str(original_query).strip()
        return RetrievalTrace(
            original_query=str(original_query),
            rewritten_query=str(rewritten_query),
            rounds=rounds,
            domains=selected,
            hits=hits,
        )


RAGRuntime = RetrievalRuntime


__all__ = ["RAGRuntime", "RetrievalRuntime", "RetrievalTrace"]
