"""Shared knowledge-memory naming facade.

KnowledgeMemory is the semantic name for the global RAG memory. LongTermMemory
is preserved as a compatibility alias for existing imports and monkeypatches.
"""

from memory.long_term import (
    EmbeddingBackend,
    HashEmbeddingBackend,
    KnowledgeMemory,
    LongTermMemory,
    OpenAIEmbeddingBackend,
    SentenceTransformerEmbeddingBackend,
    create_embedding_backend,
)

__all__ = [
    "EmbeddingBackend",
    "HashEmbeddingBackend",
    "KnowledgeMemory",
    "LongTermMemory",
    "OpenAIEmbeddingBackend",
    "SentenceTransformerEmbeddingBackend",
    "create_embedding_backend",
]
