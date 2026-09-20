from __future__ import annotations

import json

from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from rag.build import _terms, build_indexes
from rag.chunking import chunk_document, read_source, stable_chunk_id, stable_document_id
from rag.embeddings import FakeEmbeddingBackend


def _write_text_pdf(path):
    writer = PdfWriter()
    page = writer.add_blank_page(width=300, height=300)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})}
    )
    stream = DecodedStreamObject()
    stream.set_data(b"BT /F1 12 Tf 30 250 Td (PDF extracted text) Tj ET")
    page[NameObject("/Contents")] = writer._add_object(stream)
    with path.open("wb") as handle:
        writer.write(handle)


def test_structure_aware_context_keeps_original_content_and_heading_hierarchy():
    parsed = chunk_document(
        read_source_text("# Guide\n\n## Install\n\nRun the installer.", source="guide.md"),
    )

    chunk = parsed.chunks[0]
    assert chunk["heading_path"] == ["Guide", "Install"]
    assert chunk["content"] == "Run the installer."
    assert chunk["content"] != chunk["retrieval_text"]
    assert all(value in chunk["retrieval_text"] for value in ("Domain: Agent Engineering", "agent_engineering", "Document: Guide", "Section: Guide > Install"))


def test_ids_are_stable_and_change_when_chunk_content_changes():
    document_id = stable_document_id("apple_support", "guide.md")
    assert document_id == stable_document_id("apple_support", "guide.md")
    assert stable_chunk_id(document_id, 0, "same", ["Guide"]) == stable_chunk_id(document_id, 0, "same", ["Guide"])
    assert stable_chunk_id(document_id, 0, "same", ["Guide"]) != stable_chunk_id(document_id, 0, "changed", ["Guide"])


def test_markdown_frontmatter_becomes_source_metadata_not_a_content_chunk(tmp_path):
    path = tmp_path / "apple.md"
    path.write_text(
        '---\ntitle: "Refund policy"\nsource_url: "https://support.apple.com/refund"\ndoc_type: "退款政策"\nlanguage: "zh-CN"\n---\n\n# Refund policy\n\nReturn details.',
        encoding="utf-8",
    )
    document = read_source(path, domain="apple_support")
    parsed = chunk_document(document)
    assert document.source_url == "https://support.apple.com/refund"
    assert document.source_type == "退款政策"
    assert document.language == "zh-CN"
    assert parsed.chunks[0]["content"] == "Return details."
    assert parsed.chunks[0]["source_url"] == document.source_url


def test_builds_isolated_dense_and_sparse_domain_artifacts_with_url_stub_and_pdf(tmp_path):
    apple = tmp_path / "apple"
    agent = tmp_path / "agent"
    apple.mkdir()
    agent.mkdir()
    (apple / "support.md").write_text("# Apple Support\n\n## Returns\n\nReturn policy.", encoding="utf-8")
    (agent / "stub.txt").write_text("https://example.com/real-source\n", encoding="utf-8")
    _write_text_pdf(agent / "guide.pdf")

    results = build_indexes(
        domain="all",
        output_dir=tmp_path / "indexes",
        apple_sources=apple,
        agent_sources=agent,
        metadata_dir=tmp_path / "metadata",
        embedding_backend=FakeEmbeddingBackend(8),
        model_name="BAAI/bge-m3",
        reset=True,
    )
    assert {result.domain for result in results} == {"apple_support", "agent_engineering"}

    apple_manifest = json.loads((tmp_path / "indexes/apple_support/manifest.json").read_text(encoding="utf-8"))
    agent_manifest = json.loads((tmp_path / "indexes/agent_engineering/manifest.json").read_text(encoding="utf-8"))
    assert apple_manifest["counts"] == {"documents": 1, "chunks": 1, "unresolved": 0, "failed": 0}
    assert agent_manifest["counts"]["unresolved"] == 1
    assert agent_manifest["unresolved"][0]["source_url"] == "https://example.com/real-source"
    assert agent_manifest["dimension"] == 8
    assert apple_manifest["source_types"] == ["markdown"]
    assert agent_manifest["counts"]["failed"] == 0
    assert agent_manifest["build_version"] == "offline-rag-v1"
    assert (tmp_path / "indexes/apple_support/index.faiss").exists()
    assert (tmp_path / "indexes/apple_support/corpus.jsonl").exists()
    assert (tmp_path / "indexes/apple_support/bm25_index.json").exists()
    chunks = [json.loads(line) for line in (tmp_path / "indexes/agent_engineering/chunks.jsonl").read_text(encoding="utf-8").splitlines()]
    assert any("PDF extracted text" in chunk["content"] for chunk in chunks)
    assert all(chunk["domain"] == "agent_engineering" for chunk in chunks)


def test_sparse_tokens_preserve_chinese_words_and_technical_hyphens():
    tokens = _terms("退款政策退款 BGE-M3 MCP Claude Code")
    assert "退款" in tokens
    assert "政策" in tokens
    assert "bge-m3" in tokens
    assert "m3" not in tokens
    assert tokens.count("退款") == 2


def test_sparse_artifact_retains_term_frequency(tmp_path):
    apple = tmp_path / "apple"
    apple.mkdir()
    (apple / "support.md").write_text("# Support\n\n退款政策 退款。", encoding="utf-8")
    build_indexes(
        domain="apple_support",
        output_dir=tmp_path / "indexes",
        apple_sources=apple,
        agent_sources=tmp_path / "missing-agent",
        metadata_dir=tmp_path / "metadata",
        embedding_backend=FakeEmbeddingBackend(4),
        model_name="BAAI/bge-m3",
        reset=True,
    )
    artifact = json.loads((tmp_path / "indexes/apple_support/bm25_index.json").read_text(encoding="utf-8"))
    assert artifact["term_frequencies"][0]["退款"] == 2
    assert artifact["postings"]["退款"][0]["tf"] == 2
    assert artifact["document_frequency"]["退款"] == 1


def test_manifest_distinguishes_actual_fake_backend_from_bge_target(tmp_path):
    apple = tmp_path / "apple"
    apple.mkdir()
    (apple / "support.md").write_text("# Support\n\nRefund policy.", encoding="utf-8")
    build_indexes(
        domain="apple_support",
        output_dir=tmp_path / "indexes",
        apple_sources=apple,
        agent_sources=tmp_path / "missing-agent",
        metadata_dir=tmp_path / "metadata",
        embedding_backend=FakeEmbeddingBackend(4),
        model_name="BAAI/bge-m3",
        reset=True,
    )
    manifest = json.loads((tmp_path / "indexes/apple_support/manifest.json").read_text(encoding="utf-8"))
    assert manifest["actual_embedding_backend"] == "fake"
    assert manifest["actual_embedding_model"] == "fake"
    assert manifest["actual_embedding_dimension"] == 4
    assert manifest["target_embedding_model"] == "BAAI/bge-m3"
    assert manifest["artifact_kind"] == "dry_run"


def test_build_continues_after_failed_source_and_reports_it(tmp_path):
    agent = tmp_path / "agent"
    agent.mkdir()
    (agent / "broken.pdf").write_bytes(b"not a pdf")
    (agent / "good.md").write_text("# Good\n\nReadable content.", encoding="utf-8")
    result = build_indexes(
        domain="agent_engineering",
        output_dir=tmp_path / "indexes",
        apple_sources=tmp_path / "missing-apple",
        agent_sources=agent,
        metadata_dir=tmp_path / "metadata",
        embedding_backend=FakeEmbeddingBackend(4),
        model_name="BAAI/bge-m3",
        reset=True,
    )[0]
    manifest = json.loads((tmp_path / "indexes/agent_engineering/manifest.json").read_text(encoding="utf-8"))
    assert result.documents == 1
    assert result.failed == 1
    assert manifest["counts"]["failed"] == 1
    assert manifest["failed_sources"][0]["source"] == "broken.pdf"
    assert result.chunks == 1


def test_rebuild_is_idempotent_and_changed_content_gets_new_chunk_id(tmp_path):
    source = tmp_path / "sources"
    source.mkdir()
    path = source / "doc.md"
    path.write_text("# Title\n\nOriginal text.", encoding="utf-8")
    kwargs = {
        "domain": "apple_support",
        "output_dir": tmp_path / "indexes",
        "apple_sources": source,
        "agent_sources": tmp_path / "missing-agent",
        "metadata_dir": tmp_path / "metadata",
        "embedding_backend": FakeEmbeddingBackend(4),
        "model_name": "fake",
        "reset": True,
    }
    build_indexes(**kwargs)
    chunks_path = tmp_path / "indexes/apple_support/chunks.jsonl"
    first = chunks_path.read_bytes()
    first_id = json.loads(first.decode(encoding="utf-8"))["chunk_id"]
    build_indexes(**kwargs)
    assert chunks_path.read_bytes() == first
    path.write_text("# Title\n\nChanged text.", encoding="utf-8")
    build_indexes(**kwargs)
    assert json.loads(chunks_path.read_text(encoding="utf-8"))["chunk_id"] != first_id


def read_source_text(text: str, source: str):
    from rag.chunking import SourceDocument

    return SourceDocument(source=source, content=text, domain="agent_engineering", title="Guide")
