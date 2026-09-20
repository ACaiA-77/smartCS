from __future__ import annotations

import json

import pytest

from mcp.mcp_server import MCPToolServer, create_default_tools
from mcp import create_server
from rag.build import build_indexes
from rag.dense_retriever import ArtifactValidationError, DenseRetriever
from rag.embeddings import FakeEmbeddingBackend
from rag.fusion import reciprocal_rank_fusion
from rag.models import RetrievalHit
from rag.retriever import HybridRetriever
from rag.reranker import CrossEncoderReranker, FakeReranker
from rag.runtime import RetrievalRuntime
from rag.sparse_retriever import SparseRetriever


@pytest.fixture
def round2_artifacts(tmp_path):
    apple = tmp_path / "apple"
    agent = tmp_path / "agent"
    apple.mkdir()
    agent.mkdir()
    (apple / "refund.md").write_text(
        "# Apple Refund\n\n## Policy\n\nApple refund policy allows a request from purchase history.",
        encoding="utf-8",
    )
    (agent / "hooks.md").write_text(
        "# Agent Hooks\n\n## Events\n\nAgent hooks match events and execute handlers.",
        encoding="utf-8",
    )
    backend = FakeEmbeddingBackend(8)
    build_indexes(
        domain="all",
        output_dir=tmp_path / "indexes",
        apple_sources=apple,
        agent_sources=agent,
        metadata_dir=tmp_path / "metadata",
        embedding_backend=backend,
        reset=True,
    )
    return tmp_path / "indexes", backend


def test_round2_artifacts_require_explicit_dry_run_and_match_dense_backend(round2_artifacts):
    root, backend = round2_artifacts
    with pytest.raises(ArtifactValidationError, match="dry-run"):
        DenseRetriever(root, domain="apple_support", embedding_backend=backend)
    retriever = DenseRetriever(
        root, domain="apple_support", embedding_backend=backend, allow_dry_run=True
    )
    assert retriever.search("refund policy", top_k=1)[0].domain == "apple_support"
    with pytest.raises(ArtifactValidationError, match="dimension"):
        DenseRetriever(
            root,
            domain="apple_support",
            embedding_backend=FakeEmbeddingBackend(4),
            allow_dry_run=True,
        )


def test_rerankers_use_retrieval_text_with_content_fallback():
    candidate = RetrievalHit(
        "chunk-1",
        "agent_engineering",
        1,
        0.1,
        "guide.md",
        "generic",
        retrieval_text="contextual hook event",
    )
    assert FakeReranker().rerank("hook event", [candidate], 1)[0].rerank_score == 2

    class RecordingCrossEncoder:
        def __init__(self):
            self.pairs = []

        def predict(self, pairs):
            self.pairs = pairs
            return [1.0 for _ in pairs]

    model = RecordingCrossEncoder()
    CrossEncoderReranker(model=model).rerank("query", [candidate], 1)
    assert model.pairs == [("query", "contextual hook event")]


def test_production_artifact_rejects_fake_reranker(round2_artifacts):
    root, backend = round2_artifacts
    manifest_path = root / "apple_support" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.update(
        {
            "artifact_kind": "production",
            "actual_embedding_backend": "sentence_transformers",
            "embedding_backend": "sentence_transformers",
            "actual_embedding_model": "production-test",
            "embedding_model": "production-test",
            "model": "production-test",
        }
    )
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    backend.backend_name = "sentence_transformers"
    backend.model_name = "production-test"
    with pytest.raises(ArtifactValidationError, match="FakeReranker"):
        HybridRetriever(
            root,
            embedding_backend=backend,
            reranker=FakeReranker(),
        ).retrieve("refund", domains=["apple_support"])


def test_explicit_missing_artifact_root_fails_closed(tmp_path, monkeypatch):
    missing = tmp_path / "missing-rag-root"
    monkeypatch.setenv("RAG_INDEX_ROOT", str(missing))
    from memory.long_term import LongTermMemory

    memory = LongTermMemory(embedding_dim=8)
    with pytest.raises(ArtifactValidationError, match="artifact root"):
        memory.get_retriever().retrieve("query")


def test_bm25_uses_tf_idf_and_document_length(round2_artifacts):
    root, _ = round2_artifacts
    sparse = SparseRetriever(root, domain="apple_support", allow_dry_run=True)
    hits = sparse.search("refund policy", top_k=1)
    assert hits and hits[0].score > 0
    artifact = json.loads(
        (root / "apple_support" / "bm25_index.json").read_text(encoding="utf-8")
    )
    assert artifact["document_frequency"]["refund"] == 1
    assert artifact["term_frequencies"][0]["refund"] >= 1


def test_rrf_deduplicates_and_preserves_ranks():
    dense = [
        RetrievalHit("a", "apple_support", 1, 0.9, "a.md", "a"),
        RetrievalHit("b", "apple_support", 2, 0.8, "b.md", "b"),
    ]
    sparse = [
        RetrievalHit("b", "apple_support", 1, 4.0, "b.md", "b"),
        RetrievalHit("c", "apple_support", 2, 3.0, "c.md", "c"),
    ]
    fused = reciprocal_rank_fusion(dense, sparse)
    assert [item.chunk_id for item in fused] == ["b", "a", "c"]
    assert fused[0].dense_rank == 2
    assert fused[0].sparse_rank == 1
    assert fused[0].rrf_score == pytest.approx(1 / 61 + 1 / 62)


def test_hybrid_domains_metadata_and_fake_reranker(round2_artifacts):
    root, backend = round2_artifacts
    reranker = FakeReranker(score_fn=lambda _query, hit: 10 if "hooks" in hit.content else 1)
    retriever = HybridRetriever(
        root,
        embedding_backend=backend,
        reranker=reranker,
        allow_dry_run=True,
    )
    both = retriever.retrieve("events", top_k=2)
    assert both and both[0].domain == "agent_engineering"
    assert both[0].source == "hooks.md"
    filtered = retriever.retrieve("refund", domains=["agent_engineering"], top_k=2)
    assert all(item.domain == "agent_engineering" for item in filtered)


def test_hybrid_retriever_uses_one_global_rank_across_domains(round2_artifacts, monkeypatch):
    root, backend = round2_artifacts

    class HitSource:
        def __init__(self, hits):
            self.hits = hits

        def search(self, _query, _top_k):
            return [RetrievalHit.from_value(hit) for hit in self.hits]

    dense = {
        "apple_support": [RetrievalHit("apple", "apple_support", 1, 0.8)],
        "agent_engineering": [RetrievalHit("agent", "agent_engineering", 1, 0.9)],
    }
    sparse = {
        "apple_support": [RetrievalHit("apple", "apple_support", 1, 0.6)],
        "agent_engineering": [RetrievalHit("agent", "agent_engineering", 1, 0.7)],
    }
    retriever = HybridRetriever(root, embedding_backend=backend, allow_dry_run=True)
    monkeypatch.setattr(retriever, "_domains", lambda _domains: ("apple_support", "agent_engineering"))
    monkeypatch.setattr(
        retriever,
        "_artifact_retrievers",
        lambda domain: (HitSource(dense[domain]), HitSource(sparse[domain])),
    )
    fused = retriever.retrieve("q", top_k=20, dense_top_k=2, sparse_top_k=2, rerank=False)
    expected = reciprocal_rank_fusion(
        retriever.dense_search("q", domains=None, top_k=2),
        retriever.sparse_search("q", domains=None, top_k=2),
        top_k=20,
    )
    assert [hit.chunk_id for hit in fused] == ["agent", "apple"]
    assert [hit.chunk_id for hit in fused] == [hit.chunk_id for hit in expected]
    assert [hit.dense_rank for hit in fused] == [1, 2]
    assert [hit.sparse_rank for hit in fused] == [1, 2]


def test_runtime_refines_once_and_never_exceeds_two_rounds():
    class StubRetriever:
        def __init__(self):
            self.calls = 0

        def retrieve(self, query, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                return []
            return [RetrievalHit("x", "apple_support", 1, 1.0, "x.md", query)]

    retriever = StubRetriever()
    trace = RetrievalRuntime(retriever, max_retrieval_rounds=9).retrieve(
        "original", "rewritten", top_k=1
    )
    assert retriever.calls == 2
    assert trace.rounds == 2
    assert trace.hits[0].content == "original"


@pytest.mark.asyncio
async def test_mcp_can_share_the_same_hybrid_retriever(round2_artifacts):
    root, backend = round2_artifacts
    shared = HybridRetriever(root, embedding_backend=backend, allow_dry_run=True)
    server = create_default_tools(MCPToolServer(), retriever=shared)
    result = await server.call_tool(
        "knowledge_search", {"query": "agent hooks", "domain": "agent_engineering"}
    )
    assert result.success is True
    assert result.result[0]["domain"] == "agent_engineering"


@pytest.mark.asyncio
async def test_public_mcp_factory_injects_the_shared_retriever():
    class StubRetriever:
        def retrieve(self, query, *, domains=None, top_k=3, rerank=True):
            assert query == "hooks"
            assert domains == ["agent_engineering"]
            assert top_k == 1
            assert rerank is True
            return [
                RetrievalHit(
                    "hook-1",
                    "agent_engineering",
                    1,
                    1.0,
                    "hooks.md",
                    "Agent hooks",
                )
            ]

    server = create_server(retriever=StubRetriever())
    response = await server.handle_jsonrpc(
        {
            "method": "tools/call",
            "params": {
                "name": "knowledge_search",
                "arguments": {
                    "query": "hooks",
                    "domain": "agent_engineering",
                    "top_k": 1,
                },
            },
        }
    )
    assert response["result"]["success"] is True
    assert response["result"]["result"][0]["source"] == "hooks.md"


def test_legacy_memory_is_not_redirected_by_default_artifact_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("RAG_INDEX_ROOT", raising=False)
    artifact_dir = tmp_path / "vector_store" / "rag_indexes" / "apple_support"
    artifact_dir.mkdir(parents=True)
    (artifact_dir / "manifest.json").write_text("{}", encoding="utf-8")

    from memory.long_term import LongTermMemory

    memory = LongTermMemory(embedding_dim=8)
    memory.add_document("legacy account recovery", "account.md")
    retriever = memory.get_retriever()
    assert retriever.is_artifact_mode is False
    assert retriever.retrieve("account recovery", top_k=1)[0].source == "account.md"
