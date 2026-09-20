"""Offline, domain-separated RAG corpus preparation."""

from .build import BuildResult, build_indexes
from .dense_retriever import ArtifactValidationError, DenseRetriever
from .models import KnowledgeChunk, KnowledgeDocument, RetrievalHit
from .embeddings import BGE_M3_MODEL, FakeEmbeddingBackend, create_embedding_backend
from .retriever import HybridRetriever
from .reranker import CrossEncoderReranker, FakeReranker, RERANKER_MODEL
from .sparse_retriever import SparseRetriever

__all__ = [
    "BGE_M3_MODEL",
    "BuildResult",
    "ArtifactValidationError",
    "DenseRetriever",
    "FakeEmbeddingBackend",
    "FakeReranker",
    "CrossEncoderReranker",
    "HybridRetriever",
    "KnowledgeChunk",
    "KnowledgeDocument",
    "RetrievalHit",
    "RERANKER_MODEL",
    "SparseRetriever",
    "build_indexes",
    "create_embedding_backend",
]
