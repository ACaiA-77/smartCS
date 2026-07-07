from __future__ import annotations

import json

import numpy as np

from memory.long_term import LongTermMemory
from scripts.ingest_knowledge_base import ingest_knowledge_base


class TinyEmbeddingBackend:
    dimension = 4

    def embed_text(self, text: str) -> np.ndarray:
        vec = np.zeros(self.dimension, dtype=np.float32)
        lower = text.lower()
        if "product" in lower or "return" in lower:
            vec[0] = 1.0
        elif "refund" in lower:
            vec[1] = 1.0
        else:
            vec[3] = 1.0
        vec /= np.linalg.norm(vec)
        return vec

    def embed_batch(self, texts: list[str]) -> list[np.ndarray]:
        return [self.embed_text(text) for text in texts]


def test_ingest_knowledge_base_saves_index_and_metadata(tmp_path):
    kb = tmp_path / "kb"
    kb.mkdir()
    (kb / "product.md").write_text("# Product A\n\nproduct return is 3.5 percent.", encoding="utf-8")
    index_path = tmp_path / "vector_store" / "faiss_index"

    result = ingest_knowledge_base(
        kb_dir=str(kb),
        index_path=str(index_path),
        embedding_backend=TinyEmbeddingBackend(),
    )

    assert result.loaded_count == 1
    assert result.total_documents == 1
    assert index_path.exists()
    assert index_path.with_suffix(".meta.json").exists()

    metadata = json.loads(index_path.with_suffix(".meta.json").read_text(encoding="utf-8"))
    assert metadata[0]["metadata"]["source_path"].endswith("product.md")
    assert metadata[0]["metadata"]["chunk_id"]

    reloaded = LongTermMemory(index_path=str(index_path), embedding_backend=TinyEmbeddingBackend())
    results = reloaded.search("product return", top_k=1)
    assert results[0]["source"] == "product.md"


def test_ingest_knowledge_base_reset_rebuilds_from_requested_directory(tmp_path):
    first_kb = tmp_path / "first_kb"
    second_kb = tmp_path / "second_kb"
    first_kb.mkdir()
    second_kb.mkdir()
    (first_kb / "old.md").write_text("old product return policy", encoding="utf-8")
    (second_kb / "new.md").write_text("new refund policy", encoding="utf-8")
    index_path = tmp_path / "vector_store" / "faiss_index"

    ingest_knowledge_base(
        kb_dir=str(first_kb),
        index_path=str(index_path),
        embedding_backend=TinyEmbeddingBackend(),
    )
    result = ingest_knowledge_base(
        kb_dir=str(second_kb),
        index_path=str(index_path),
        embedding_backend=TinyEmbeddingBackend(),
        reset=True,
    )

    assert result.total_documents == 1
    metadata = json.loads(index_path.with_suffix(".meta.json").read_text(encoding="utf-8"))
    assert metadata[0]["source"] == "new.md"
