from __future__ import annotations

import numpy as np

from memory.long_term import LongTermMemory


class TinyEmbeddingBackend:
    dimension = 4

    def embed_text(self, text: str) -> np.ndarray:
        vec = np.zeros(self.dimension, dtype=np.float32)
        lower = text.lower()
        if "product" in lower or "理财" in text or "收益" in text:
            vec[0] = 1.0
        if "refund" in lower or "退款" in text:
            vec[1] = 1.0
        if "account" in lower or "开户" in text:
            vec[2] = 1.0
        if not vec.any():
            vec[3] = 1.0
        vec /= np.linalg.norm(vec)
        return vec

    def embed_batch(self, texts: list[str]) -> list[np.ndarray]:
        return [self.embed_text(text) for text in texts]


def test_search_uses_injected_embedding_backend_and_returns_metadata(tmp_path):
    mem = LongTermMemory(index_path=str(tmp_path / "faiss_index"), embedding_backend=TinyEmbeddingBackend())
    mem.add_document("理财产品A年化收益率为3.5%-5.2%", source="product.md", metadata={"section": "rates"})
    mem.add_document("退款政策为7天内可申请", source="refund.md", metadata={"section": "refund"})

    results = mem.search("理财产品A收益率", top_k=2)

    assert results[0]["source"] == "product.md"
    assert results[0]["metadata"]["section"] == "rates"
    assert results[0]["score"] > 0.9


def test_load_knowledge_base_is_idempotent_and_replaces_changed_documents(tmp_path):
    kb = tmp_path / "kb"
    kb.mkdir()
    product = kb / "product.md"
    product.write_text("# Product A\n\nproduct return is 3.5 percent.", encoding="utf-8")

    mem = LongTermMemory(index_path=str(tmp_path / "faiss_index"), embedding_backend=TinyEmbeddingBackend())

    assert mem.load_knowledge_base(str(kb)) == 1
    assert mem.load_knowledge_base(str(kb)) == 0
    assert len(mem.documents) == 1

    metadata = mem.documents[0]["metadata"]
    assert metadata["chunk_id"]
    assert metadata["content_hash"]
    assert metadata["source_path"] == str(product)
    assert metadata["updated_at"]

    old_chunk_id = metadata["chunk_id"]
    old_hash = metadata["content_hash"]

    product.write_text("# Product A\n\nproduct return is 4.2 percent.", encoding="utf-8")

    assert mem.load_knowledge_base(str(kb)) == 1
    assert len(mem.documents) == 1
    assert "4.2" in mem.documents[0]["content"]
    assert mem.documents[0]["metadata"]["content_hash"] != old_hash
    assert mem.documents[0]["metadata"]["chunk_id"] != old_chunk_id


def test_min_score_filters_weak_semantic_matches(tmp_path):
    mem = LongTermMemory(
        index_path=str(tmp_path / "faiss_index"),
        embedding_backend=TinyEmbeddingBackend(),
        min_score=0.8,
    )
    mem.add_document("退款政策为7天内可申请", source="refund.md")

    assert mem.search("完全无关的问题", top_k=3) == []


def test_load_knowledge_base_reads_markdown_and_adds_chunk_metadata(tmp_path):
    kb = tmp_path / "kb"
    kb.mkdir()
    (kb / "product.md").write_text("# 产品A\n\n理财产品A年化收益率为3.5%-5.2%。", encoding="utf-8")
    (kb / "refund.txt").write_text("退款政策：7天内可申请。", encoding="utf-8")

    mem = LongTermMemory(index_path=str(tmp_path / "faiss_index"), embedding_backend=TinyEmbeddingBackend())
    count = mem.load_knowledge_base(str(kb))

    assert count == 2
    assert {doc["source"] for doc in mem.documents} == {"product.md", "refund.txt"}
    assert all("chunk_index" in doc["metadata"] for doc in mem.documents)
    assert all("doc_id" in doc["metadata"] for doc in mem.documents)


def test_save_and_reload_preserves_documents_and_index(tmp_path):
    index_path = tmp_path / "faiss_index"
    mem = LongTermMemory(index_path=str(index_path), embedding_backend=TinyEmbeddingBackend())
    mem.add_document("理财产品A年化收益率为3.5%-5.2%", source="product.md")
    mem.save()

    reloaded = LongTermMemory(index_path=str(index_path), embedding_backend=TinyEmbeddingBackend())
    results = reloaded.search("理财产品A收益率", top_k=1)

    assert results[0]["source"] == "product.md"
