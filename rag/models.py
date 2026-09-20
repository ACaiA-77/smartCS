"""Small data contracts shared by the offline RAG builder."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class KnowledgeDocument:
    source: str
    content: str
    domain: str
    title: str
    source_url: str = ""
    source_type: str = ""
    language: str = ""
    unresolved: bool = False
    unresolved_reason: str = ""

    @property
    def doc_type(self) -> str:
        """Compatibility spelling used by older ingestion code."""

        return self.source_type


@dataclass(frozen=True)
class KnowledgeChunk:
    chunk_id: str
    document_id: str
    domain: str
    content: str
    retrieval_text: str
    title: str
    heading_path: list[str] = field(default_factory=list)
    source: str = ""
    source_url: str = ""
    source_type: str = ""
    language: str = ""
    chunk_index: int = 0

    @property
    def doc_type(self) -> str:
        return self.source_type

    def to_dict(self) -> dict[str, Any]:
        return {
            "chunk_id": self.chunk_id,
            "document_id": self.document_id,
            "domain": self.domain,
            "content": self.content,
            "retrieval_text": self.retrieval_text,
            "title": self.title,
            "heading_path": list(self.heading_path),
            "source": self.source,
            "source_url": self.source_url,
            "source_type": self.source_type,
            "doc_type": self.source_type,
            "language": self.language,
            "chunk_index": self.chunk_index,
        }

    def __getitem__(self, key: str) -> Any:
        """Keep the old dict-shaped test and serialization call sites working."""

        return self.to_dict()[key]


@dataclass
class RetrievalHit:
    """One retrieval result shared by dense, sparse, fusion and reranking."""

    chunk_id: str
    domain: str
    rank: int
    score: float
    source: str = ""
    content: str = ""
    heading_path: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    retrieval_text: str = ""
    dense_rank: int | None = None
    sparse_rank: int | None = None
    rrf_score: float | None = None
    dense_score: float | None = None
    sparse_score: float | None = None
    rerank_score: float | None = None

    def to_dict(self) -> dict[str, Any]:
        result = dict(self.metadata)
        result.update(
            {
                "chunk_id": self.chunk_id,
                "domain": self.domain,
                "rank": self.rank,
                "score": float(self.score),
                "source": self.source,
                "content": self.content,
                "retrieval_text": self.retrieval_text or str(self.metadata.get("retrieval_text") or self.content),
                "heading_path": list(self.heading_path),
            }
        )
        for name in (
            "dense_rank",
            "sparse_rank",
            "rrf_score",
            "dense_score",
            "sparse_score",
            "rerank_score",
        ):
            value = getattr(self, name)
            if value is not None:
                result[name] = float(value) if name.endswith("score") else value
        result["metadata"] = dict(self.metadata)
        return result

    @classmethod
    def from_value(cls, value: "RetrievalHit | dict[str, Any]") -> "RetrievalHit":
        if isinstance(value, cls):
            return value
        metadata = dict(value.get("metadata") or {})
        return cls(
            chunk_id=str(value.get("chunk_id") or metadata.get("chunk_id") or value.get("id", "")),
            domain=str(value.get("domain") or metadata.get("domain") or ""),
            rank=int(value.get("rank", 0) or 0),
            score=float(value.get("score", 0.0) or 0.0),
            source=str(value.get("source") or metadata.get("source") or ""),
            content=str(value.get("content") or ""),
            retrieval_text=str(
                value.get("retrieval_text")
                or metadata.get("retrieval_text")
                or value.get("content")
                or ""
            ),
            heading_path=list(value.get("heading_path") or metadata.get("heading_path") or []),
            metadata=metadata,
            dense_rank=value.get("dense_rank"),
            sparse_rank=value.get("sparse_rank"),
            rrf_score=value.get("rrf_score"),
            dense_score=value.get("dense_score"),
            sparse_score=value.get("sparse_score"),
            rerank_score=value.get("rerank_score"),
        )

    def get(self, key: str, default: Any = None) -> Any:
        return self.to_dict().get(key, default)

    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]
