"""BM25 retrieval over the Round 1 sparse artifacts."""

from __future__ import annotations

import math
from typing import Any

from .build import _terms
from .dense_retriever import DomainArtifacts, load_domain_artifacts
from .models import RetrievalHit


class SparseRetriever:
    def __init__(
        self,
        artifact_path: str | Any,
        *,
        domain: str | None = None,
        allow_dry_run: bool = False,
        artifacts: DomainArtifacts | None = None,
    ) -> None:
        self.artifacts = artifacts or load_domain_artifacts(
            artifact_path, domain=domain, allow_dry_run=allow_dry_run
        )
        self.index = self.artifacts.bm25
        self.k1 = float(self.index.get("k1", 1.5))
        self.b = float(self.index.get("b", 0.75))
        self._chunks = self.artifacts.chunks

    @property
    def domain(self) -> str:
        return self.artifacts.domain

    def search(self, query: str, top_k: int = 5) -> list[RetrievalHit]:
        if not str(query).strip() or top_k <= 0 or not self._chunks:
            return []
        terms = _terms(query)
        if not terms:
            return []
        lengths = self.index["document_lengths"]
        avgdl = float(self.index.get("average_document_length") or 0.0)
        document_frequency = self.index["document_frequency"]
        term_frequencies = self.index["term_frequencies"]
        total_documents = len(self._chunks)
        scores = [0.0] * total_documents
        query_terms = set(terms)
        for term in query_terms:
            df = int(document_frequency.get(term, 0))
            if not df:
                continue
            # Robertson/Sparck Jones IDF with the +1 stabilizer.
            idf = math.log((total_documents - df + 0.5) / (df + 0.5) + 1.0)
            for document_index, frequencies in enumerate(term_frequencies):
                tf = int(frequencies.get(term, 0))
                if not tf:
                    continue
                normalization = 1.0 - self.b
                if avgdl:
                    normalization += self.b * float(lengths[document_index]) / avgdl
                scores[document_index] += idf * (
                    tf * (self.k1 + 1.0) / (tf + self.k1 * normalization)
                )
        ranked = sorted(
            ((score, index) for index, score in enumerate(scores) if score > 0.0),
            key=lambda item: (-item[0], item[1]),
        )[:top_k]
        results: list[RetrievalHit] = []
        for rank, (score, index) in enumerate(ranked, start=1):
            chunk = self._chunks[index]
            results.append(
                RetrievalHit(
                    chunk_id=str(chunk["chunk_id"]),
                    domain=self.domain,
                    rank=rank,
                    score=float(score),
                    source=str(chunk.get("source", "")),
                    content=str(chunk.get("content", "")),
                    retrieval_text=str(chunk.get("retrieval_text") or chunk.get("content", "")),
                    heading_path=list(chunk.get("heading_path") or []),
                    metadata=dict(chunk),
                    sparse_rank=rank,
                    sparse_score=float(score),
                )
            )
        return results


__all__ = ["SparseRetriever"]
