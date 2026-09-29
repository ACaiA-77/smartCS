from __future__ import annotations

import json
import shutil
from collections import Counter
from pathlib import Path

import pytest

from rag.evaluation.evaluator import evaluate_variants
from rag.evaluation.models import BenchmarkQuery
from rag.models import RetrievalHit
from scripts.evaluate_rag_retrieval import validate_benchmark_manifest
from scripts.build_rag_benchmark import build


def test_checked_in_benchmark_is_balanced_and_auditable():
    root = Path("benchmarks/rag")
    queries = [json.loads(line) for line in (root / "queries.jsonl").read_text(encoding="utf-8").splitlines()]
    qrels = [json.loads(line) for line in (root / "qrels.jsonl").read_text(encoding="utf-8").splitlines()]
    manifest = json.loads((root / "benchmark_manifest.json").read_text(encoding="utf-8"))
    assert len(queries) == 60
    assert len(qrels) == manifest["qrel_count"] == 95
    assert manifest["benchmark_version"] == "rag-round3-v4-qrel-audited"
    assert manifest["chunking_version"] == "structure-context-v1"
    assert {row["domain"] for row in queries} == {"apple_support", "agent_engineering"}
    assert manifest["domains"] == {"apple_support": 30, "agent_engineering": 30}
    assert manifest["query_kinds"] == {
        "apple_support": {"confusing": 10, "lexical": 10, "semantic": 10},
        "agent_engineering": {"confusing": 10, "lexical": 10, "semantic": 10},
    }
    assert manifest["relevance_distribution"] == dict(Counter(str(row["relevance"]) for row in qrels))
    assert len({(row["query_id"], row["chunk_id"]) for row in qrels}) == len(qrels)
    assert all(
        set(row) >= {"query_id", "chunk_id", "relevance", "domain", "source", "heading_path", "rationale"}
        for row in qrels
    )
    assert {row["relevance"] for row in qrels} == {1, 2}
    assert all(
        any(row["query_id"] == query["query_id"] and row["relevance"] >= 1 for row in qrels)
        for query in queries
    )
    apple_028 = [row for row in qrels if row["query_id"] == "apple_028"]
    assert [(row["chunk_id"], row["relevance"]) for row in apple_028] == [
        ("cb626faa924378d10a7b0420", 2)
    ]
    assert not any(row["query_id"] in {
        "apple_001", "apple_009", "apple_011", "apple_015", "apple_016",
        "apple_018", "apple_023", "apple_026", "apple_029",
    } and row["relevance"] == 1 for row in qrels)
    index_root = Path(manifest["index_root"])
    chunks_by_domain = {
        domain: {
            json.loads(line)["chunk_id"]
            for line in (index_root / domain / "chunks.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        }
        for domain in ("apple_support", "agent_engineering")
    }
    assert all(row["chunk_id"] in chunks_by_domain[row["domain"]] for row in qrels)
    assert all(row.get("authoring") == "manual-curated-v2" for row in queries)
    assert len({row["query"] for row in queries}) == 60
    assert {row["kind"] for row in queries} == {"semantic", "lexical", "confusing"}
    assert next(row for row in queries if row["query_id"] == "apple_029")["query"] == (
        "通过 Apple 按月或按年付费的 AppleCare 计划取消后，保障会持续到什么时候？"
    )
    # Compound questions can require multiple supporting chunks without a direct answer.
    assert {row["chunk_id"]: row["relevance"] for row in qrels if row["query_id"] == "apple_007"} == {
        "9612d7764daa609cc0ab6841": 1,
        "e2dce93fe5b10a44fc1666ad": 1,
        "61da22fc10c2e2d1dcaea7b7": 1,
    }


def test_canonical_builder_reproduces_checked_in_manifest(tmp_path):
    source = Path("benchmarks/rag")
    for name in ("queries.jsonl", "qrels.jsonl"):
        shutil.copyfile(source / name, tmp_path / name)
    expected = json.loads((source / "benchmark_manifest.json").read_text(encoding="utf-8"))
    assert build(Path("artifacts/rag_round3/production_indexes"), tmp_path) == expected
    assert json.loads((tmp_path / "benchmark_manifest.json").read_text(encoding="utf-8")) == expected


@pytest.mark.parametrize("version", ["different-version", None, ""])
def test_builder_rejects_inconsistent_or_missing_chunking_version(tmp_path, version):
    source = Path("benchmarks/rag")
    for name in ("queries.jsonl", "qrels.jsonl"):
        shutil.copyfile(source / name, tmp_path / name)
    for domain in ("apple_support", "agent_engineering"):
        target = tmp_path / "indexes" / domain
        target.mkdir(parents=True)
        shutil.copyfile(Path("artifacts/rag_round3/production_indexes") / domain / "chunks.jsonl", target / "chunks.jsonl")
        (target / "manifest.json").write_text(
            json.dumps({"chunking_version": "structure-context-v1" if domain == "apple_support" else version}),
            encoding="utf-8",
        )
    with pytest.raises(ValueError, match="chunking_version"):
        build(tmp_path / "indexes", tmp_path)
    assert not (tmp_path / "benchmark_manifest.json").exists()


class _StubRetriever:
    def __init__(self):
        self.calls = []

    def dense_search(self, query, *, domains, top_k):
        self.calls.append(("dense", query, domains, top_k))
        return [RetrievalHit("a", "apple_support", 1, 1.0, content="a")]

    def sparse_search(self, query, *, domains, top_k):
        self.calls.append(("bm25", query, domains, top_k))
        return [RetrievalHit("b", "apple_support", 1, 1.0, content="b")]

    def rerank(self, query, candidates, top_k):
        self.calls.append(("rerank", query, top_k, len(candidates)))
        return list(reversed(candidates))[:top_k]


def test_variants_use_same_fixed_query_and_candidate_limits():
    retriever = _StubRetriever()
    query = BenchmarkQuery("q1", "fixed query", "apple_support", "semantic", {"a": 2})
    report = evaluate_variants(retriever, [query])
    assert set(report["overall"]) == {"dense", "bm25", "hybrid_rrf", "hybrid_rerank"}
    assert [call[3] for call in retriever.calls if call[0] in {"dense", "bm25"}] == [20, 20]
    assert [call[2:] for call in retriever.calls if call[0] == "rerank"] == [(10, 2)]
    assert all(call[1] == "fixed query" for call in retriever.calls)
    assert all(call[2] is None for call in retriever.calls if call[0] in {"dense", "bm25"})
    assert all("wrong_domain_rate@10" in row["variants"]["dense"] for row in report["per_query"])


class _CrossDomainRetriever(_StubRetriever):
    def dense_search(self, query, *, domains, top_k):
        return [
            RetrievalHit("local", "agent_engineering", 1, 0.9, content="local"),
            RetrievalHit("global", "apple_support", 1, 0.8, content="global"),
        ]

    def sparse_search(self, query, *, domains, top_k):
        return [
            RetrievalHit("global", "apple_support", 1, 0.7, content="global"),
            RetrievalHit("local", "agent_engineering", 1, 0.6, content="local"),
        ]


def test_headline_rankings_keep_global_cross_domain_candidates():
    report = evaluate_variants(
        _CrossDomainRetriever(),
        [BenchmarkQuery("q1", "fixed query", "apple_support", "semantic", {"global": 2})],
    )
    row = report["per_query"][0]
    assert row["rankings"]["dense"] == ["local", "global"]
    assert row["rankings"]["bm25"] == ["global", "local"]
    assert row["variants"]["dense"]["wrong_domain_rate@1"] == 1.0


def test_benchmark_manifest_hash_mismatch_fails_closed(tmp_path):
    source = Path("benchmarks/rag/benchmark_manifest.json")
    manifest = json.loads(source.read_text(encoding="utf-8"))
    manifest["source_sha256"]["apple_support"] = "bad"
    root = tmp_path / "benchmark"
    root.mkdir()
    (root / source.name).write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="source hash mismatch"):
        validate_benchmark_manifest(root, Path(manifest["index_root"]))
