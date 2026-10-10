"""
Long-term memory backed by a vector index.

The production path is:
source documents -> chunks with metadata -> embedding backend -> FAISS index.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

import numpy as np

from rag.model_devices import (
    EMBEDDING_DEVICE_ENV,
    device_from_env,
    require_device_available,
    verify_and_log_model,
)

try:
    import faiss
except ImportError:
    faiss = None


class EmbeddingBackend(Protocol):
    """Minimal interface for local or remote embedding providers."""

    dimension: int

    def embed_text(self, text: str) -> np.ndarray:
        ...

    def embed_batch(self, texts: list[str]) -> list[np.ndarray]:
        ...


def _normalize_vector(vector: np.ndarray) -> np.ndarray:
    vec = np.asarray(vector, dtype=np.float32)
    norm = float(np.linalg.norm(vec))
    if norm == 0.0:
        return vec
    return vec / norm


class HashEmbeddingBackend:
    """
    Deterministic local fallback embedding.

    This is not a real semantic model, but it is stable, offline, and safer than
    random vectors for tests and demos. Set EMBEDDING_BACKEND=sentence_transformers
    or openai for stronger retrieval.
    """

    def __init__(self, dimension: int = 1536):
        self.dimension = dimension

    def embed_text(self, text: str) -> np.ndarray:
        vec = np.zeros(self.dimension, dtype=np.float32)
        tokens = self._tokens(text)
        if not tokens:
            tokens = [text.strip() or "<empty>"]

        for token in tokens:
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            idx = int.from_bytes(digest[:4], "big") % self.dimension
            sign = 1.0 if digest[4] % 2 == 0 else -1.0
            vec[idx] += sign

        return _normalize_vector(vec)

    def embed_batch(self, texts: list[str]) -> list[np.ndarray]:
        return [self.embed_text(text) for text in texts]

    @staticmethod
    def _tokens(text: str) -> list[str]:
        normalized = text.lower()
        words = re.findall(r"[a-z0-9_]+", normalized)
        cjk_chars = re.findall(r"[\u4e00-\u9fff]", normalized)
        cjk_bigrams = [normalized[i : i + 2] for i in range(max(len(normalized) - 1, 0))]
        cjk_bigrams = [item for item in cjk_bigrams if re.search(r"[\u4e00-\u9fff]", item)]
        return words + cjk_chars + cjk_bigrams


class SentenceTransformerEmbeddingBackend:
    """Local embedding backend using sentence-transformers when installed."""

    def __init__(self, model_name: str):
        device = device_from_env(EMBEDDING_DEVICE_ENV)
        require_device_available(device)
        from sentence_transformers import SentenceTransformer

        self.model_name = model_name
        self._model = SentenceTransformer(model_name, **({"device": device} if device is not None else {}))
        verify_and_log_model(self._model, model_name, device)
        if hasattr(self._model, "get_embedding_dimension"):
            self.dimension = int(self._model.get_embedding_dimension())
        else:
            self.dimension = int(self._model.get_sentence_embedding_dimension())

    def embed_text(self, text: str) -> np.ndarray:
        vec = self._model.encode(text, normalize_embeddings=True)
        return _normalize_vector(vec)

    def embed_batch(self, texts: list[str]) -> list[np.ndarray]:
        vectors = self._model.encode(texts, normalize_embeddings=True)
        return [_normalize_vector(vec) for vec in vectors]


class OpenAIEmbeddingBackend:
    """Remote embedding backend for deployments that prefer API-managed models."""

    def __init__(
        self,
        model_name: str,
        api_key: str | None = None,
        base_url: str | None = None,
        dimension: int | None = None,
    ):
        from langchain_openai import OpenAIEmbeddings

        self.model_name = model_name
        self._embeddings = OpenAIEmbeddings(
            model=model_name,
            api_key=api_key or os.getenv("OPENAI_API_KEY"),
            base_url=base_url or os.getenv("OPENAI_BASE_URL"),
        )
        self.dimension = dimension or self._detect_dimension()

    def _detect_dimension(self) -> int:
        sample = self._embeddings.embed_query("dimension probe")
        return len(sample)

    def embed_text(self, text: str) -> np.ndarray:
        return _normalize_vector(np.asarray(self._embeddings.embed_query(text), dtype=np.float32))

    def embed_batch(self, texts: list[str]) -> list[np.ndarray]:
        vectors = self._embeddings.embed_documents(texts)
        return [_normalize_vector(np.asarray(vec, dtype=np.float32)) for vec in vectors]


def create_embedding_backend(embedding_dim: int = 1536) -> EmbeddingBackend:
    backend = os.getenv("EMBEDDING_BACKEND", "hash").strip().lower()
    model_name = os.getenv("EMBEDDING_MODEL", "BAAI/bge-small-zh-v1.5")

    if backend in {"sentence_transformers", "local"}:
        return SentenceTransformerEmbeddingBackend(model_name)

    if backend in {"openai", "remote"}:
        return OpenAIEmbeddingBackend(
            model_name=os.getenv("EMBEDDING_MODEL", "text-embedding-3-small"),
            dimension=int(os.getenv("EMBEDDING_DIM", str(embedding_dim))),
        )

    if backend == "auto":
        # An explicit local-model device is a deployment requirement, not an
        # invitation to silently replace a failed model with hash embeddings.
        if device_from_env(EMBEDDING_DEVICE_ENV) is not None:
            return SentenceTransformerEmbeddingBackend(model_name)
        try:
            return SentenceTransformerEmbeddingBackend(model_name)
        except Exception:
            return HashEmbeddingBackend(embedding_dim)

    return HashEmbeddingBackend(embedding_dim)


class KnowledgeMemory:
    """
    Shared FAISS-based knowledge memory for RAG retrieval.

    This is intentionally for global knowledge only. User-owned profile and
    episodic memory live in memory.user_memory and never reuse these retriever
    ids, vectors, or FAISS artifacts.
    """

    def __init__(
        self,
        index_path: str = "./vector_store/faiss_index",
        embedding_dim: int = 1536,
        embedding_backend: EmbeddingBackend | None = None,
        min_score: float = -1.0,
    ):
        self.index_path = Path(index_path)
        self.embedding_backend = embedding_backend or create_embedding_backend(embedding_dim)
        self.embedding_dim = int(self.embedding_backend.dimension)
        self.min_score = min_score
        self._documents: list[dict[str, Any]] = []
        self._index = None
        self._retriever = None
        self._retriever_root = None
        self._retriever_sparse_mode = None
        self._init_index()

    @property
    def documents(self) -> list[dict[str, Any]]:
        return list(self._documents)

    def get_retriever(
        self,
        artifact_root: str | None = None,
        *,
        use_env: bool = True,
    ):
        """Return a retriever, optionally ignoring process-wide RAG settings."""
        from rag.retriever import HybridRetriever
        from rag.reranker import create_reranker

        configured_root = artifact_root
        if configured_root is None and use_env:
            configured_root = os.getenv("RAG_INDEX_ROOT")
        sparse_mode = (
            os.getenv("RAG_SPARSE_MODE", "global_corpus_v1").strip()
            if artifact_root is None and use_env and configured_root is not None
            else "domain_local_v1"
        )
        if (
            artifact_root is None
            and use_env
            and self._retriever is not None
            and self._retriever_root == configured_root
            and self._retriever_sparse_mode == sparse_mode
        ):
            return self._retriever
        explicit_artifacts = artifact_root is not None or configured_root is not None
        root = configured_root
        reranker_backend = os.getenv("RAG_RERANKER_BACKEND", "fake") if use_env else "fake"
        reranker = create_reranker(reranker_backend)
        retriever = HybridRetriever(
            root,
            embedding_backend=self.embedding_backend,
            reranker=reranker,
            allow_dry_run=os.getenv("RAG_ALLOW_DRY_RUN", "false").lower() in {"1", "true", "yes", "on"},
            legacy_memory=None if explicit_artifacts else self,
            sparse_mode=sparse_mode,
        )
        if artifact_root is None and use_env:
            self._retriever = retriever
            self._retriever_root = configured_root
            self._retriever_sparse_mode = sparse_mode
        return retriever

    def _init_index(self) -> None:
        if faiss is None:
            self._index = None
            self._load_metadata()
            return

        metadata_path = self.index_path.with_suffix(".meta.json")
        if self.index_path.exists():
            try:
                loaded_index = faiss.read_index(str(self.index_path))
                if int(loaded_index.d) != self.embedding_dim:
                    raise ValueError(
                        f"FAISS dimension {loaded_index.d} does not match embedding dimension {self.embedding_dim}"
                    )
                self._index = loaded_index
                if metadata_path.exists():
                    self._load_metadata()
                return
            except Exception:
                self._documents = []

        self._index = faiss.IndexFlatIP(self.embedding_dim)

    def _load_metadata(self) -> None:
        metadata_path = self.index_path.with_suffix(".meta.json")
        if metadata_path.exists():
            with open(metadata_path, "r", encoding="utf-8") as f:
                self._documents = json.load(f)

    def _embed_text(self, text: str) -> np.ndarray:
        return _normalize_vector(self.embedding_backend.embed_text(text))

    def _embed_batch(self, texts: list[str]) -> list[np.ndarray]:
        if hasattr(self.embedding_backend, "embed_batch"):
            vectors = self.embedding_backend.embed_batch(texts)
        else:
            vectors = [self.embedding_backend.embed_text(text) for text in texts]
        return [_normalize_vector(vector) for vector in vectors]

    def add_document(self, content: str, source: str = "", metadata: dict | None = None) -> str:
        doc_metadata = dict(metadata or {})
        doc_id = doc_metadata.get("doc_id") or self._stable_doc_id(content, source)
        doc_metadata["doc_id"] = doc_id
        doc_metadata.setdefault("content_hash", self._content_hash(content))
        doc_metadata.setdefault("chunk_id", self._stable_chunk_id(doc_id, content, doc_metadata))
        doc_metadata.setdefault("updated_at", self._now_iso())

        doc = {
            "id": doc_id,
            "content": content,
            "source": source,
            "metadata": doc_metadata,
        }
        self._documents.append(doc)

        if self._index is not None:
            embedding = self._embed_text(content)
            self._index.add(embedding.reshape(1, -1))

        return doc_id

    def remove_documents_by_source_path(self, source_path: str) -> int:
        before = len(self._documents)
        self._documents = [
            doc
            for doc in self._documents
            if doc.get("metadata", {}).get("source_path") != source_path
        ]
        removed = before - len(self._documents)
        if removed:
            self._rebuild_index()
        return removed

    def add_documents_batch(self, documents: list[dict]) -> list[str]:
        prepared_docs: list[dict[str, Any]] = []
        doc_ids: list[str] = []

        for item in documents:
            content = item.get("content", "")
            source = item.get("source", "")
            metadata = dict(item.get("metadata", {}))
            doc_id = metadata.get("doc_id") or self._stable_doc_id(content, source)
            metadata["doc_id"] = doc_id
            prepared_docs.append(
                {
                    "id": doc_id,
                    "content": content,
                    "source": source,
                    "metadata": metadata,
                }
            )
            doc_ids.append(doc_id)

        self._documents.extend(prepared_docs)

        if self._index is not None and prepared_docs:
            vectors = self._embed_batch([doc["content"] for doc in prepared_docs])
            matrix = np.vstack(vectors).astype(np.float32)
            self._index.add(matrix)

        return doc_ids

    def search(self, query: str, top_k: int = 5) -> list[dict]:
        if self._index is None or not self._documents:
            return self._fallback_search(query, top_k)

        limit = min(top_k, len(self._documents))
        query_vec = self._embed_text(query).reshape(1, -1)
        scores, indices = self._index.search(query_vec, limit)

        results = []
        for score, idx in zip(scores[0], indices[0]):
            if idx < 0 or idx >= len(self._documents):
                continue
            score_value = float(score)
            if score_value < self.min_score:
                continue
            doc = self._documents[idx].copy()
            doc["metadata"] = dict(doc.get("metadata", {}))
            doc["score"] = score_value
            results.append(doc)

        return results

    def _fallback_search(self, query: str, top_k: int) -> list[dict]:
        scored = []
        query_terms = set(HashEmbeddingBackend._tokens(query))

        for doc in self._documents:
            content_terms = set(HashEmbeddingBackend._tokens(doc["content"]))
            if not query_terms or not content_terms:
                continue
            overlap = len(query_terms & content_terms)
            score = overlap / max(len(query_terms), 1)
            if score >= self.min_score and score > 0:
                scored.append((score, doc))

        scored.sort(key=lambda x: x[0], reverse=True)
        results = []
        for score, doc in scored[:top_k]:
            result = doc.copy()
            result["metadata"] = dict(result.get("metadata", {}))
            result["score"] = float(score)
            results.append(result)
        return results

    def save(self) -> None:
        self.index_path.parent.mkdir(parents=True, exist_ok=True)

        if self._index is not None:
            faiss.write_index(self._index, str(self.index_path))

        metadata_path = self.index_path.with_suffix(".meta.json")
        with open(metadata_path, "w", encoding="utf-8") as f:
            json.dump(self._documents, f, ensure_ascii=False, indent=2)

    def load_knowledge_base(self, kb_dir: str) -> int:
        kb_path = Path(kb_dir)
        if not kb_path.exists():
            return 0

        count = 0
        supported_files = sorted(
            path for path in kb_path.rglob("*") if path.suffix.lower() in {".md", ".txt"}
        )
        for file_path in supported_files:
            content = file_path.read_text(encoding="utf-8")
            source_path = str(file_path)
            document_hash = self._content_hash(content)
            existing_docs = [
                doc
                for doc in self._documents
                if doc.get("metadata", {}).get("source_path") == source_path
            ]
            if existing_docs and all(
                doc.get("metadata", {}).get("document_hash") == document_hash
                for doc in existing_docs
            ):
                continue
            if existing_docs:
                self.remove_documents_by_source_path(source_path)

            chunks = self._chunk_text(content)
            source_doc_id = self._stable_doc_id(source_path, "document")
            updated_at = self._now_iso()

            for chunk_index, chunk in enumerate(chunks):
                chunk_hash = self._content_hash(chunk)
                self.add_document(
                    content=chunk,
                    source=file_path.name,
                    metadata={
                        "file": str(file_path),
                        "source_path": source_path,
                        "doc_id": source_doc_id,
                        "chunk_id": self._stable_chunk_id(
                            source_doc_id,
                            chunk,
                            {"chunk_index": chunk_index},
                        ),
                        "content_hash": chunk_hash,
                        "document_hash": document_hash,
                        "chunk_index": chunk_index,
                        "chunk_count": len(chunks),
                        "file_type": file_path.suffix.lower().lstrip("."),
                        "updated_at": updated_at,
                    },
                )
                count += 1

        return count

    def _rebuild_index(self) -> None:
        if faiss is None:
            self._index = None
            return

        self._index = faiss.IndexFlatIP(self.embedding_dim)
        if not self._documents:
            return

        vectors = self._embed_batch([doc["content"] for doc in self._documents])
        matrix = np.vstack(vectors).astype(np.float32)
        self._index.add(matrix)

    @staticmethod
    def _stable_doc_id(content: str, source: str = "") -> str:
        digest = hashlib.sha256(f"{source}\n{content}".encode("utf-8")).hexdigest()
        return digest[:16]

    @staticmethod
    def _content_hash(content: str) -> str:
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    @staticmethod
    def _stable_chunk_id(doc_id: str, content: str, metadata: dict[str, Any]) -> str:
        chunk_index = metadata.get("chunk_index", "")
        digest = hashlib.sha256(f"{doc_id}\n{chunk_index}\n{content}".encode("utf-8")).hexdigest()
        return digest[:16]

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _chunk_text(text: str, chunk_size: int = 512, overlap: int = 128) -> list[str]:
        paragraphs = text.split("\n\n")
        chunks = []
        current_chunk = ""

        for para in paragraphs:
            para = para.strip()
            if not para:
                continue

            if len(current_chunk) + len(para) <= chunk_size:
                current_chunk += para + "\n\n"
                continue

            if current_chunk:
                chunks.append(current_chunk.strip())
                overlap_text = current_chunk[-overlap:] if len(current_chunk) > overlap else current_chunk
                current_chunk = overlap_text + para + "\n\n"
                continue

            sentences = re.split(r"(?<=[。！？.!?])\s*", para)
            for sentence in sentences:
                sentence = sentence.strip()
                if not sentence:
                    continue
                if len(current_chunk) + len(sentence) <= chunk_size:
                    current_chunk += sentence
                else:
                    if current_chunk:
                        chunks.append(current_chunk.strip())
                    current_chunk = sentence

        if current_chunk.strip():
            chunks.append(current_chunk.strip())

        return chunks if chunks else [text[:chunk_size]]


# Backward-compatible import name used by agents, tests, and monkeypatch paths.
LongTermMemory = KnowledgeMemory
