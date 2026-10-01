from __future__ import annotations

import builtins
import hashlib
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from rag.build import _term_frequencies, build_indexes
from rag.dense_retriever import load_domain_artifacts
from rag.embeddings import FakeEmbeddingBackend
from rag.global_sparse import build_global_sparse_artifact
from scripts.rebuild_rag_sparse import main, rebuild_sparse_artifacts


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _tree_hashes(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): _sha256(path)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _blocked_jieba_import(real_import):
    def blocked_import(name, *args, **kwargs):
        if name == "jieba":
            raise ImportError("blocked for fallback comparison")
        return real_import(name, *args, **kwargs)

    return blocked_import


def _fallback_term_frequencies(text: str) -> dict[str, int]:
    with patch("builtins.__import__", side_effect=_blocked_jieba_import(builtins.__import__)):
        return _term_frequencies(text)


def _build_fixture(root: Path) -> Path:
    apple = root / "apple_sources"
    agent = root / "agent_sources"
    indexes = root / "indexes"
    apple.mkdir(parents=True)
    agent.mkdir(parents=True)
    (apple / "repair.md").write_text(
        "# 维修\n苹果手机维修政策说明，苹果手机维修流程和退款政策。",
        encoding="utf-8",
    )
    (agent / "retrieval.md").write_text(
        "# 检索\n智能客服检索系统使用稀疏索引和向量索引处理中文知识。",
        encoding="utf-8",
    )
    with patch("builtins.__import__", side_effect=_blocked_jieba_import(builtins.__import__)):
        build_indexes(
            domain="all",
            output_dir=indexes,
            apple_sources=apple,
            agent_sources=agent,
            embedding_backend=FakeEmbeddingBackend(8),
        )
    build_global_sparse_artifact(indexes, allow_dry_run=True)
    return indexes


def test_jieba_tokenization_changes_meaningful_chinese_fallback(monkeypatch):
    text = "苹果手机维修政策"
    jieba_terms = _term_frequencies(text)

    fallback_terms = _fallback_term_frequencies(text)

    assert jieba_terms != fallback_terms
    assert "手机" in jieba_terms
    assert fallback_terms.get("手") == 1 and fallback_terms.get("机") == 1


def test_rebuild_sparse_preserves_dense_and_chunks_rebuilds_sparse_and_loads(tmp_path):
    source = _build_fixture(tmp_path / "source")
    before = _tree_hashes(source)
    dense_hashes = {
        domain: _sha256(source / domain / "index.faiss")
        for domain in ("apple_support", "agent_engineering")
    }
    chunk_hashes = {
        domain: _sha256(source / domain / "chunks.jsonl")
        for domain in ("apple_support", "agent_engineering")
    }

    output = tmp_path / "rebuilt"
    result = rebuild_sparse_artifacts(source, output, allow_dry_run=True)

    assert result["output_root"] == str(output)
    assert result["total_chunks"] == 2
    assert result["global_sparse"]["documents"] == 2
    assert result["tokenizer"]["jieba_version"]
    assert result["tokenizer"]["dictionary_sha256"]
    assert _tree_hashes(source) == before

    changed_domains = []
    for domain in ("apple_support", "agent_engineering"):
        assert Path(result["domains"][domain]["path"]).is_dir()
        assert _sha256(output / domain / "index.faiss") == dense_hashes[domain]
        assert _sha256(output / domain / "chunks.jsonl") == chunk_hashes[domain]
        manifest = json.loads((output / domain / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["sparse_tokenizer_provenance"]["tokenizer"].startswith("jieba.lcut")
        artifacts = load_domain_artifacts(output, domain=domain, allow_dry_run=True)
        assert artifacts.bm25["chunk_ids"] == [chunk["chunk_id"] for chunk in artifacts.chunks]
        rebuilt_corpus = (output / domain / "corpus.jsonl").read_text(encoding="utf-8")
        old_corpus = (source / domain / "corpus.jsonl").read_text(encoding="utf-8")
        rebuilt_bm25 = (output / domain / "bm25_index.json").read_text(encoding="utf-8")
        old_bm25 = (source / domain / "bm25_index.json").read_text(encoding="utf-8")
        if rebuilt_corpus != old_corpus and rebuilt_bm25 != old_bm25:
            changed_domains.append(domain)
    assert changed_domains

    old_artifacts = load_domain_artifacts(source, domain="apple_support", allow_dry_run=True)
    new_artifacts = load_domain_artifacts(output, domain="apple_support", allow_dry_run=True)
    retrieval_text = old_artifacts.chunks[0]["retrieval_text"]
    assert old_artifacts.bm25["term_frequencies"][0] == _fallback_term_frequencies(retrieval_text)
    assert new_artifacts.bm25["term_frequencies"][0] == _term_frequencies(retrieval_text)
    assert old_artifacts.bm25["term_frequencies"][0] != new_artifacts.bm25["term_frequencies"][0]
    assert "手机" in new_artifacts.bm25["term_frequencies"][0]
    assert old_artifacts.bm25["term_frequencies"][0].get("手", 0) > 0
    assert old_artifacts.bm25["term_frequencies"][0].get("机", 0) > 0
    assert "手机" not in old_artifacts.bm25["term_frequencies"][0]

    assert Path(result["global_sparse"]["path"]).is_dir()
    global_manifest = json.loads((output / "global_sparse" / "manifest.json").read_text(encoding="utf-8"))
    assert global_manifest["document_count"] == 2


def test_cli_fails_when_output_exists(tmp_path, capsys):
    source = _build_fixture(tmp_path / "source")
    output = tmp_path / "exists"
    output.mkdir()

    code = main(["--input-root", str(source), "--output-root", str(output), "--allow-dry-run"])

    captured = capsys.readouterr()
    assert code == 2
    assert "FileExistsError" in captured.err
