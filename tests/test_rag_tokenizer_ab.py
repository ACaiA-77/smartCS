"""Guard fair fallback-vs-jieba sparse evaluation on frozen chunks/dense vectors."""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
from pathlib import Path

import pytest

from rag.build import _write_bm25, _write_corpus, build_indexes
from rag.embeddings import FakeEmbeddingBackend
from rag.global_sparse import build_global_sparse_artifact
from rag.reranker import FakeReranker
from rag.retriever import HybridRetriever
from scripts import evaluate_rag_tokenizer_ab as comparison


@pytest.fixture
def comparison_fixture(tmp_path: Path, monkeypatch):
    jieba = pytest.importorskip("jieba")
    apple, agent = tmp_path / "apple", tmp_path / "agent"
    apple.mkdir()
    agent.mkdir()
    (apple / "guide.md").write_text("# 退款政策\n\n知识库检索系统支持退款流程。", encoding="utf-8")
    (agent / "guide.md").write_text("# Agent 工作流\n\n自动化 Agent 工具流程。", encoding="utf-8")
    baseline, candidate, benchmark = (tmp_path / name for name in ("baseline", "candidate", "benchmark"))
    benchmark.mkdir()
    backend = FakeEmbeddingBackend(8)
    monkeypatch.setitem(sys.modules, "jieba", None)
    build_indexes(domain="all", output_dir=baseline, apple_sources=apple, agent_sources=agent,
                  metadata_dir=tmp_path / "empty", embedding_backend=backend)
    monkeypatch.setitem(sys.modules, "jieba", jieba)
    shutil.copytree(baseline, candidate)
    for domain in ("apple_support", "agent_engineering"):
        chunks = [json.loads(line) for line in (candidate / domain / "chunks.jsonl").read_text(encoding="utf-8").splitlines()]
        _write_corpus(candidate / domain / "corpus.jsonl", chunks)
        _write_bm25(candidate / domain / "bm25_index.json", chunks)
    chunk_id = json.loads(next((baseline / "apple_support" / "chunks.jsonl").open(encoding="utf-8")))["chunk_id"]
    (benchmark / "queries.jsonl").write_text(
        json.dumps({"query_id": "q1", "query": "知识库检索系统如何退款", "domain": "apple_support", "kind": "lexical"},
                   ensure_ascii=False) + "\n", encoding="utf-8")
    (benchmark / "qrels.jsonl").write_text(
        json.dumps({"query_id": "q1", "chunk_id": chunk_id, "relevance": 2}) + "\n", encoding="utf-8")
    source_sha256 = {domain: hashlib.sha256((baseline / domain / "chunks.jsonl").read_bytes()).hexdigest()
                     for domain in ("apple_support", "agent_engineering")}
    (benchmark / "benchmark_manifest.json").write_text(json.dumps({"source_sha256": source_sha256}), encoding="utf-8")
    monkeypatch.setattr(comparison, "SentenceTransformerEmbeddingBackend", lambda: backend)
    monkeypatch.setattr(comparison, "HybridRetriever",
                        lambda *args, **kwargs: HybridRetriever(*args, **kwargs, allow_dry_run=True))
    return baseline, candidate, benchmark


def test_compares_three_variants_without_relabeling_old_rerank(comparison_fixture, tmp_path: Path):
    baseline, candidate, benchmark = comparison_fixture
    before = (baseline / "apple_support" / "bm25_index.json").read_bytes()
    result = comparison.run(benchmark_root=benchmark, baseline_root=baseline,
                            candidate_root=candidate, output_root=tmp_path / "result")
    assert result["query_count"] == 1
    assert result["reranker_status"].startswith("not_rerun")
    assert set(result["arms"]["fallback"]["overall"]) == {"dense", "bm25", "hybrid_rrf"}
    assert (result["arms"]["fallback"]["per_query"][0]["rankings"]["dense"]
            == result["arms"]["jieba"]["per_query"][0]["rankings"]["dense"])
    assert (baseline / "apple_support" / "bm25_index.json").read_bytes() == before
    assert (tmp_path / "result" / "comparison.json").is_file()


def test_rejects_existing_diagnostic_output_without_modifying_it(comparison_fixture, tmp_path: Path):
    baseline, candidate, benchmark = comparison_fixture
    output = tmp_path / "existing"
    output.mkdir()
    report = output / "comparison.json"
    report.write_bytes(b"historical evidence")
    with pytest.raises(FileExistsError, match="overwrite"):
        comparison.run(benchmark_root=benchmark, baseline_root=baseline,
                       candidate_root=candidate, output_root=output)
    assert report.read_bytes() == b"historical evidence"


def test_local_rerank_fingerprint_does_not_require_global_sparse(comparison_fixture):
    baseline, candidate, benchmark = comparison_fixture
    assert not (baseline / "global_sparse").exists()
    fingerprint = comparison._run_fingerprint(
        benchmark_root=benchmark, baseline_root=baseline, candidate_root=candidate,
        sparse_mode="domain_local_v1", qrel_groups_path=None, jieba_version="0.42.1",
    )
    assert len(fingerprint["files_sha256"]) == 19
    assert not any("global_sparse" in path for path in fingerprint["files_sha256"])
    with pytest.raises(FileNotFoundError):
        comparison._run_fingerprint(
            benchmark_root=benchmark, baseline_root=baseline, candidate_root=candidate,
            sparse_mode="global_corpus_v1", qrel_groups_path=None, jieba_version="0.42.1",
        )


def test_rejects_changed_dense_index_before_measurement(comparison_fixture, tmp_path: Path):
    baseline, candidate, benchmark = comparison_fixture
    with (candidate / "apple_support" / "index.faiss").open("ab") as handle:
        handle.write(b"x")
    with pytest.raises(ValueError, match="index.faiss changed"):
        comparison.run(benchmark_root=benchmark, baseline_root=baseline,
                       candidate_root=candidate, output_root=tmp_path / "result")


def test_rerank_checkpoint_resume_and_input_hash_guard(comparison_fixture, tmp_path: Path, monkeypatch):
    # Small fixture with a counted test-only reranker; production runs use the real model.
    import jieba

    baseline, candidate, benchmark = comparison_fixture
    backend = comparison.SentenceTransformerEmbeddingBackend()
    backend.backend_name = "sentence_transformers"
    backend.model_name = "BAAI/bge-m3"
    for root in (baseline, candidate):
        for domain in ("apple_support", "agent_engineering"):
            manifest_path = root / domain / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest.update({
                "artifact_kind": "production", "actual_embedding_backend": "sentence_transformers",
                "embedding_backend": "sentence_transformers", "actual_embedding_model": "BAAI/bge-m3",
                "embedding_model": "BAAI/bge-m3", "model": "BAAI/bge-m3",
            })
            if root == candidate:
                manifest["sparse_tokenizer_provenance"] = {"jieba_version": jieba.__version__}
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        build_global_sparse_artifact(root)

    calls = []

    class CountedReranker:
        def rerank(self, query, candidates, top_k=10):
            calls.append(query)
            return FakeReranker().rerank(query, candidates, top_k)

    monkeypatch.setattr(comparison, "CrossEncoderReranker", CountedReranker)
    monkeypatch.setattr(comparison, "validate", lambda **kwargs: {
        "status": "ready", "fake_embedding": False, "fake_reranker": False, "errors": [],
    })
    output = tmp_path / "rerank"
    result = comparison.run(benchmark_root=benchmark, baseline_root=baseline, candidate_root=candidate,
                            output_root=output, sparse_mode="global_corpus_v1", include_rerank=True)
    assert calls == ["知识库检索系统如何退款"] * 2  # one full rerank per arm
    assert result["reranker_status"] == "real_cross_encoder_complete"
    assert "hybrid_rerank" in result["arms"]["jieba"]["overall"]
    assert (output / "progress" / "001.json").is_file()
    assert result["arms"]["fallback"]["per_query"][0]["rankings"]["dense"] == result["arms"]["jieba"]["per_query"][0]["rankings"]["dense"]
    comparison.run(benchmark_root=benchmark, baseline_root=baseline, candidate_root=candidate,
                   output_root=output, sparse_mode="global_corpus_v1", include_rerank=True, resume=True)
    assert len(calls) == 2  # completed query reused, no model inference
    with pytest.raises(FileExistsError, match="overwrite"):
        comparison.run(benchmark_root=benchmark, baseline_root=baseline, candidate_root=candidate,
                       output_root=output, sparse_mode="global_corpus_v1", include_rerank=True)
    qrel = (benchmark / "qrels.jsonl").read_text(encoding="utf-8")
    (benchmark / "qrels.jsonl").write_text(qrel + qrel, encoding="utf-8")
    with pytest.raises(ValueError, match="run_manifest"):
        comparison.run(benchmark_root=benchmark, baseline_root=baseline, candidate_root=candidate,
                       output_root=output, sparse_mode="global_corpus_v1", include_rerank=True, resume=True)
