"""Injectable embedding backends for the offline index builder."""

from __future__ import annotations

import hashlib
import re
from typing import Protocol

import numpy as np

from .model_devices import (
    EMBEDDING_DEVICE_ENV,
    device_from_env,
    require_device_available,
    verify_and_log_model,
)

BGE_M3_MODEL = "BAAI/bge-m3"
BGE_M3_DIMENSION = 1024


class EmbeddingBackend(Protocol):
    dimension: int

    def embed_batch(self, texts: list[str]) -> list[np.ndarray]:
        ...


def _normalize(vector: np.ndarray) -> np.ndarray:
    value = np.asarray(vector, dtype=np.float32)
    norm = float(np.linalg.norm(value))
    return value if norm == 0.0 else value / norm


class FakeEmbeddingBackend:
    """Small deterministic backend for tests and local dry runs."""

    def __init__(self, dimension: int = 8):
        if dimension < 1:
            raise ValueError("embedding dimension must be positive")
        self.dimension = dimension
        self.backend_name = "fake"
        self.model_name = "fake"

    def embed_text(self, text: str) -> np.ndarray:
        vector = np.zeros(self.dimension, dtype=np.float32)
        tokens = re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]", text.lower()) or ["<empty>"]
        for token in tokens:
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            index = int.from_bytes(digest[:4], "big") % self.dimension
            vector[index] += 1.0 if digest[4] % 2 else -1.0
        return _normalize(vector)

    def embed_batch(self, texts: list[str]) -> list[np.ndarray]:
        return [self.embed_text(text) for text in texts]


class SentenceTransformerEmbeddingBackend:
    """Local production backend; model loading is intentionally injectable."""

    def __init__(self, model_name: str = BGE_M3_MODEL):
        device = device_from_env(EMBEDDING_DEVICE_ENV)
        require_device_available(device)
        from sentence_transformers import SentenceTransformer

        self.model_name = model_name
        self.backend_name = "sentence_transformers"
        self._model = SentenceTransformer(model_name, **({"device": device} if device is not None else {}))
        verify_and_log_model(self._model, model_name, device)
        self.dimension = int(self._model.get_sentence_embedding_dimension())

    def embed_batch(self, texts: list[str]) -> list[np.ndarray]:
        vectors = self._model.encode(texts, normalize_embeddings=True)
        return [_normalize(vector) for vector in vectors]

    def embed_text(self, text: str) -> np.ndarray:
        return self.embed_batch([text])[0]


def create_embedding_backend(
    backend: str = "fake",
    *,
    model_name: str = BGE_M3_MODEL,
    dimension: int = BGE_M3_DIMENSION,
) -> EmbeddingBackend:
    """Create an explicit backend; the CLI defaults to fake to stay offline."""

    name = backend.strip().lower()
    if name in {"fake", "hash"}:
        return FakeEmbeddingBackend(dimension)
    if name in {"sentence_transformers", "local", "bge-m3"}:
        return SentenceTransformerEmbeddingBackend(model_name)
    raise ValueError(f"unsupported embedding backend: {backend}")
