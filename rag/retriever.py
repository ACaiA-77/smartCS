"""Online hybrid retrieval shared by the Agent and MCP knowledge tool."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Iterable

from .dense_retriever import ArtifactValidationError, DenseRetriever
from .fusion import reciprocal_rank_fusion
from .global_sparse import GlobalSparseRetriever
from .models import RetrievalHit
from .reranker import FakeReranker, Reranker
from .sparse_retriever import SparseRetriever


def global_ranked_candidates(
    hits: Iterable[RetrievalHit], *, top_k: int, rank_field: str
) -> list[RetrievalHit]:
    """Merge domain-local hits into one deterministic global candidate list."""

    candidates: dict[str, RetrievalHit] = {}
    for value in hits:
        hit = RetrievalHit.from_value(value)
        if not hit.chunk_id:
            continue
        current = candidates.get(hit.chunk_id)
        if current is None or float(hit.score) > float(current.score):
            candidates[hit.chunk_id] = hit
    ordered = sorted(
        candidates.values(),
        key=lambda hit: (-float(hit.score), hit.domain, hit.chunk_id),
    )[: max(0, top_k)]
    for rank, hit in enumerate(ordered, start=1):
        hit.rank = rank
        setattr(hit, rank_field, rank)
        if rank_field == "dense_rank":
            hit.dense_score = hit.score
        elif rank_field == "sparse_rank":
            hit.sparse_score = hit.score
    return ordered


class HybridRetriever:
    """Dense + BM25 + RRF + Cross-Encoder retrieval over one or more domains."""

    def __init__(
        self,
        artifact_root: str | Path | None = None,
        *,
        embedding_backend: Any | None = None,
        reranker: Reranker | None = None,
        allow_dry_run: bool = False,
        rrf_k: int = 60,
        legacy_memory: Any | None = None,
        domains: Iterable[str] | None = None,
        sparse_mode: str = "domain_local_v1",
    ) -> None:
        if sparse_mode not in {"domain_local_v1", "global_corpus_v1"}:
            raise ValueError(f"unsupported sparse_mode: {sparse_mode}")
        self._force_artifacts = artifact_root is not None
        self.artifact_root = Path(artifact_root or "vector_store/rag_indexes")
        self.embedding_backend = embedding_backend
        self.reranker = reranker or FakeReranker()
        self.allow_dry_run = allow_dry_run
        self.rrf_k = rrf_k
        self.legacy_memory = legacy_memory
        self.default_domains = tuple(domains or ())
        self.sparse_mode = sparse_mode
        self._domain_retrievers: dict[str, tuple[DenseRetriever, SparseRetriever]] = {}
        self._global_sparse: GlobalSparseRetriever | None = None

    @classmethod
    def from_long_term_memory(
        cls,
        memory: Any,
        *,
        artifact_root: str | Path | None = None,
        embedding_backend: Any | None = None,
        reranker: Reranker | None = None,
        allow_dry_run: bool = False,
    ) -> "HybridRetriever":
        return cls(
            artifact_root,
            embedding_backend=embedding_backend,
            reranker=reranker,
            allow_dry_run=allow_dry_run,
            legacy_memory=memory,
        )

    @property
    def is_artifact_mode(self) -> bool:
        return self._force_artifacts or (
            self.legacy_memory is None and self.artifact_root.exists()
        )

    def _ensure_artifact_root(self) -> None:
        if self._force_artifacts and not self.artifact_root.is_dir():
            raise ArtifactValidationError(
                f"artifact root missing or invalid: {self.artifact_root}"
            )

    def _domains(self, domains: Iterable[str] | str | None) -> tuple[str, ...]:
        if isinstance(domains, str):
            domains = (domains,)
        if domains:
            result = tuple(dict.fromkeys(str(value).strip() for value in domains if str(value).strip()))
            if result:
                return result
        if self.default_domains:
            return self.default_domains
        if self.is_artifact_mode:
            found = tuple(
                sorted(
                    item.name
                    for item in self.artifact_root.iterdir()
                    if item.is_dir() and item.name != "global_sparse" and (item / "manifest.json").exists()
                )
            )
            if found:
                return found
        return ()

    def _artifact_retrievers(self, domain: str) -> tuple[DenseRetriever, SparseRetriever]:
        pair = self._domain_retrievers.get(domain)
        if pair is None:
            dense = DenseRetriever(
                self.artifact_root,
                domain=domain,
                embedding_backend=self.embedding_backend,
                allow_dry_run=self.allow_dry_run,
            )
            if (
                isinstance(self.reranker, FakeReranker)
                and dense.artifacts.manifest.get("artifact_kind") == "production"
            ):
                raise ArtifactValidationError(
                    "FakeReranker is only allowed for dry_run artifacts or legacy memory"
                )
            sparse = SparseRetriever(
                self.artifact_root,
                domain=domain,
                allow_dry_run=self.allow_dry_run,
                artifacts=dense.artifacts,
            )
            pair = (dense, sparse)
            self._domain_retrievers[domain] = pair
        return pair

    def _global_sparse_retriever(self) -> GlobalSparseRetriever:
        if self._global_sparse is None:
            self._global_sparse = GlobalSparseRetriever(
                self.artifact_root, allow_dry_run=self.allow_dry_run
            )
        return self._global_sparse

    @staticmethod
    def _legacy_hit(document: dict[str, Any], position: int) -> RetrievalHit:
        metadata = dict(document.get("metadata") or {})
        chunk_id = str(
            document.get("chunk_id")
            or metadata.get("chunk_id")
            or document.get("id")
            or hashlib.sha256(
                f"{document.get('source', '')}\n{document.get('content', '')}".encode("utf-8")
            ).hexdigest()[:16]
        )
        domain = str(document.get("domain") or metadata.get("domain") or "legacy")
        return RetrievalHit(
            chunk_id=chunk_id,
            domain=domain,
            rank=position,
            score=float(document.get("score", 0.0) or 0.0),
            source=str(document.get("source") or metadata.get("source") or ""),
            content=str(document.get("content") or ""),
            retrieval_text=str(
                document.get("retrieval_text")
                or metadata.get("retrieval_text")
                or document.get("content")
                or ""
            ),
            heading_path=list(document.get("heading_path") or metadata.get("heading_path") or []),
            metadata=metadata,
        )

    def _legacy_lists(
        self, query: str, top_k: int, domains: tuple[str, ...]
    ) -> tuple[list[RetrievalHit], list[RetrievalHit]]:
        if self.legacy_memory is None:
            return [], []
        documents = self.legacy_memory.search(query, top_k=max(top_k, 1))
        hits = []
        for position, document in enumerate(documents, start=1):
            hit = self._legacy_hit(document, position)
            if domains and hit.domain != "legacy" and hit.domain not in domains:
                continue
            hit.dense_rank = position
            hit.dense_score = hit.score
            hits.append(hit)
        sparse = [RetrievalHit.from_value(hit) for hit in hits]
        for position, hit in enumerate(sparse, start=1):
            hit.rank = position
            hit.sparse_rank = position
            hit.sparse_score = hit.score
        return hits, sparse

    def retrieve(
        self,
        query: str,
        *,
        domains: Iterable[str] | str | None = None,
        top_k: int = 3,
        dense_top_k: int | None = None,
        sparse_top_k: int | None = None,
        rerank: bool = True,
    ) -> list[RetrievalHit]:
        query = str(query).strip()
        if not query or top_k <= 0:
            return []
        self._ensure_artifact_root()
        selected_domains = self._domains(domains)
        dense_limit = dense_top_k or max(5, top_k * 3)
        sparse_limit = sparse_top_k or max(5, top_k * 3)
        dense_hits: list[RetrievalHit] = []
        sparse_hits: list[RetrievalHit] = []
        if self.is_artifact_mode:
            if not selected_domains:
                raise ArtifactValidationError("no artifact domains are configured")
            use_global_sparse = self.sparse_mode == "global_corpus_v1" and len(selected_domains) > 1
            for domain in selected_domains:
                dense, sparse = self._artifact_retrievers(domain)
                dense_hits.extend(dense.search(query, dense_limit))
                if not use_global_sparse:
                    sparse_hits.extend(sparse.search(query, sparse_limit))
            if use_global_sparse:
                sparse_hits = self._global_sparse_retriever().search(query, sparse_limit)
        else:
            dense_hits, sparse_hits = self._legacy_lists(query, dense_limit, selected_domains)
        dense_hits = global_ranked_candidates(
            dense_hits, top_k=dense_limit, rank_field="dense_rank"
        )
        sparse_hits = global_ranked_candidates(
            sparse_hits, top_k=sparse_limit, rank_field="sparse_rank"
        )
        fused = reciprocal_rank_fusion(
            dense_hits,
            sparse_hits,
            rrf_k=self.rrf_k,
            top_k=max(top_k * 3, top_k) if rerank else top_k,
        )
        if not rerank:
            return fused[:top_k]
        ranked = self.reranker.rerank(query, fused, top_k=top_k)
        return [RetrievalHit.from_value(item) for item in ranked]

    search = retrieve

    def dense_search(
        self, query: str, *, domains: Iterable[str] | str | None = None, top_k: int = 5
    ) -> list[RetrievalHit]:
        if not self.is_artifact_mode:
            return global_ranked_candidates(
                self._legacy_lists(query, top_k, self._domains(domains))[0],
                top_k=top_k,
                rank_field="dense_rank",
            )
        self._ensure_artifact_root()
        hits = [
            hit
            for domain in self._domains(domains)
            for hit in self._artifact_retrievers(domain)[0].search(query, top_k)
        ]
        return global_ranked_candidates(hits, top_k=top_k, rank_field="dense_rank")

    def sparse_search(
        self, query: str, *, domains: Iterable[str] | str | None = None, top_k: int = 5
    ) -> list[RetrievalHit]:
        if not self.is_artifact_mode:
            return global_ranked_candidates(
                self._legacy_lists(query, top_k, self._domains(domains))[1],
                top_k=top_k,
                rank_field="sparse_rank",
            )
        self._ensure_artifact_root()
        selected_domains = self._domains(domains)
        if self.sparse_mode == "global_corpus_v1" and len(selected_domains) > 1:
            return self._global_sparse_retriever().search(query, top_k)
        hits = [
            hit
            for domain in selected_domains
            for hit in self._artifact_retrievers(domain)[1].search(query, top_k)
        ]
        return global_ranked_candidates(hits, top_k=top_k, rank_field="sparse_rank")

    def rerank(self, query: str, candidates: list[RetrievalHit], top_k: int = 3):
        return self.reranker.rerank(query, candidates, top_k=top_k)


__all__ = ["HybridRetriever", "global_ranked_candidates"]
