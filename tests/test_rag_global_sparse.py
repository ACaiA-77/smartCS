import hashlib

import pytest

from rag.build import build_indexes
from rag.dense_retriever import ArtifactValidationError
from rag.embeddings import FakeEmbeddingBackend
from rag.global_sparse import build_global_sparse_artifact
from rag.retriever import HybridRetriever


def test_global_sparse_candidate_is_deterministic_gated_and_source_bound(tmp_path):
    apple, agent, root = (tmp_path / name for name in ("apple", "agent", "indexes"))
    apple.mkdir()
    agent.mkdir()
    (apple / "refund.md").write_text("# Refund\nApple refund policy and purchase history.", encoding="utf-8")
    (agent / "hooks.md").write_text("# Hooks\nAgent hooks and event handlers.", encoding="utf-8")
    backend = FakeEmbeddingBackend(8)
    build_indexes(domain="all", output_dir=root, apple_sources=apple, agent_sources=agent,
                  embedding_backend=backend)
    target = build_global_sparse_artifact(root, allow_dry_run=True)
    first = hashlib.sha256((target / "bm25_index.json").read_bytes()).hexdigest()
    assert build_global_sparse_artifact(root, allow_dry_run=True) == target
    assert hashlib.sha256((target / "bm25_index.json").read_bytes()).hexdigest() == first

    default = HybridRetriever(root, embedding_backend=backend, allow_dry_run=True)
    candidate = HybridRetriever(root, embedding_backend=backend, allow_dry_run=True,
                                sparse_mode="global_corpus_v1")
    assert default._domains(None) == ("agent_engineering", "apple_support")
    query = "refund hooks"
    assert candidate.sparse_search(query, top_k=2)
    assert [hit.chunk_id for hit in candidate.sparse_search(query, domains="apple_support", top_k=2)] == [
        hit.chunk_id for hit in default.sparse_search(query, domains="apple_support", top_k=2)]
    assert candidate.retrieve(query, top_k=2)

    with pytest.raises(ValueError, match="unsupported sparse_mode"):
        HybridRetriever(root, sparse_mode="typo")
    (root / "apple_support" / "chunks.jsonl").write_text("changed", encoding="utf-8")
    with pytest.raises(ArtifactValidationError, match="source changed"):
        HybridRetriever(root, sparse_mode="global_corpus_v1").sparse_search(query, top_k=2)
