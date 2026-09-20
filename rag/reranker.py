"""Injectable Cross-Encoder reranking with an offline deterministic fake."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from .build import _terms
from .models import RetrievalHit

RERANKER_MODEL = "BAAI/bge-reranker-v2-m3"


class Reranker(Protocol):
    def rerank(self, query: str, candidates: list[RetrievalHit], top_k: int = 3) -> list[RetrievalHit]:
        ...


class FakeReranker:
    """Deterministic local reranker for tests; it never downloads a model."""

    backend_name = "fake"
    model_name = "fake"

    def __init__(self, score_fn: Callable[[str, RetrievalHit], float] | None = None):
        self.score_fn = score_fn

    def _score(self, query: str, candidate: RetrievalHit) -> float:
        if self.score_fn is not None:
            return float(self.score_fn(query, candidate))
        query_terms = set(_terms(query))
        content_terms = set(_terms(candidate.retrieval_text or candidate.content))
        return float(len(query_terms & content_terms))

    def rerank(self, query: str, candidates: list[RetrievalHit], top_k: int = 3) -> list[RetrievalHit]:
        scored = []
        for position, value in enumerate(candidates):
            candidate = RetrievalHit.from_value(value)
            candidate.rerank_score = self._score(query, candidate)
            scored.append((candidate.rerank_score, position, candidate))
        scored.sort(key=lambda item: (-item[0], item[1], item[2].chunk_id))
        result = [item[2] for item in scored[: max(0, top_k)]]
        for rank, candidate in enumerate(result, start=1):
            candidate.rank = rank
            candidate.score = float(candidate.rerank_score or 0.0)
        return result


DeterministicFakeReranker = FakeReranker


class CrossEncoderReranker:
    """Production Cross-Encoder wrapper; model loading stays explicit and injectable."""

    backend_name = "sentence_transformers"

    def __init__(self, model_name: str = RERANKER_MODEL, model=None):
        self.model_name = model_name
        if model is None:
            from sentence_transformers import CrossEncoder

            model = CrossEncoder(model_name)
        self._model = model

    def rerank(self, query: str, candidates: list[RetrievalHit], top_k: int = 3) -> list[RetrievalHit]:
        if not candidates:
            return []
        pairs = [
            (query, RetrievalHit.from_value(item).retrieval_text or RetrievalHit.from_value(item).content)
            for item in candidates
        ]
        scores = self._model.predict(pairs)
        ranked = sorted(
            zip(scores, candidates), key=lambda item: (-float(item[0]), item[1].chunk_id)
        )[: max(0, top_k)]
        result = []
        for rank, (score, value) in enumerate(ranked, start=1):
            candidate = RetrievalHit.from_value(value)
            candidate.rerank_score = float(score)
            candidate.score = float(score)
            candidate.rank = rank
            result.append(candidate)
        return result


def create_reranker(
    backend: str = "fake", *, model_name: str = RERANKER_MODEL, model=None
) -> Reranker:
    name = backend.strip().lower()
    if name in {"fake", "hash", "dry_run"}:
        return FakeReranker()
    if name in {"cross_encoder", "sentence_transformers", "local"}:
        return CrossEncoderReranker(model_name=model_name, model=model)
    raise ValueError(f"unsupported reranker backend: {backend}")


__all__ = [
    "CrossEncoderReranker",
    "DeterministicFakeReranker",
    "FakeReranker",
    "RERANKER_MODEL",
    "Reranker",
    "create_reranker",
]
