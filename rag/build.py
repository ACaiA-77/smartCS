"""Build deterministic dense and sparse artifacts for the two RAG domains."""

from __future__ import annotations

import json
import re
import shutil
from collections import Counter
from datetime import datetime, timezone
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np

from .chunking import (
    CHUNKING_VERSION,
    ParsedSource,
    chunk_document,
    read_source,
    stable_document_id,
)
from .embeddings import BGE_M3_DIMENSION, BGE_M3_MODEL, EmbeddingBackend, FakeEmbeddingBackend
from .models import KnowledgeChunk

try:
    import faiss
except ImportError:  # pragma: no cover - dependency is in requirements.txt
    faiss = None

DOMAINS = ("apple_support", "agent_engineering")
_TECH_TOKEN_RE = re.compile(r"[A-Za-z0-9]+(?:[-_][A-Za-z0-9]+)*|[\u4e00-\u9fff]+")
_CJK_WORDS = tuple(
    sorted(
        {
            "退款", "政策", "申请", "条件", "流程", "订阅", "订单", "工单", "知识", "检索",
            "用户", "认证", "会话", "工具", "调用", "模型", "向量", "索引", "领域", "文档",
            "内容", "账号", "账户", "客服", "智能", "系统", "工程", "编程", "代码", "开发",
            "代理", "自动化", "恢复", "检查点", "架构", "设计", "实现", "业务", "支持", "维修",
            "服务", "付款", "支付", "购买", "取消", "苹果", "应用", "商店", "账号密码",
        },
        key=len,
        reverse=True,
    )
)


@dataclass(frozen=True)
class BuildResult:
    domain: str
    output_dir: str
    documents: int
    chunks: int
    unresolved: int
    failed: int
    index_path: str
    chunks_path: str
    corpus_path: str
    manifest_path: str


def build_indexes(
    *,
    domain: str = "all",
    output_dir: str | Path = "vector_store/rag_indexes",
    apple_sources: str | Path = "knowledge_base",
    agent_sources: str | Path = "knowledge_sources/agent_engineering",
    metadata_dir: str | Path = "knowledge_sources/metadata",
    embedding_backend: EmbeddingBackend | None = None,
    model_name: str = BGE_M3_MODEL,
    chunk_size: int = 900,
    overlap: int = 120,
    reset: bool = False,
) -> list[BuildResult]:
    if domain not in (*DOMAINS, "all"):
        raise ValueError(f"unsupported domain: {domain}")
    # The offline default is deterministic and never pulls a model.
    embedding_backend = embedding_backend or FakeEmbeddingBackend(BGE_M3_DIMENSION)
    selected = DOMAINS if domain == "all" else (domain,)
    roots = {
        "apple_support": Path(apple_sources),
        "agent_engineering": Path(agent_sources),
    }
    output_root = Path(output_dir)
    metadata = _load_metadata(Path(metadata_dir))
    results = []
    for selected_domain in selected:
        target = output_root / selected_domain
        if reset and target.exists():
            shutil.rmtree(target)
        target.mkdir(parents=True, exist_ok=True)
        parsed_sources, failed_sources = _parse_sources(
            selected_domain,
            roots[selected_domain],
            metadata,
            chunk_size=chunk_size,
            overlap=overlap,
        )
        chunks = [chunk for parsed in parsed_sources for chunk in parsed.chunks]
        unresolved = [parsed for parsed in parsed_sources if parsed.document.unresolved]
        _write_chunks(target / "chunks.jsonl", chunks)
        _write_corpus(target / "corpus.jsonl", chunks)
        _write_bm25(target / "bm25_index.json", chunks)
        index_path = target / "index.faiss"
        _write_faiss(index_path, chunks, embedding_backend)
        manifest_path = target / "manifest.json"
        manifest = {
            "manifest_version": 1,
            "domain": selected_domain,
            "embedding_backend": _backend_name(embedding_backend),
            "actual_embedding_backend": _backend_name(embedding_backend),
            "embedding_model": _actual_model_name(embedding_backend),
            "actual_embedding_model": _actual_model_name(embedding_backend),
            "embedding_dimension": int(embedding_backend.dimension),
            "actual_embedding_dimension": int(embedding_backend.dimension),
            "target_embedding_model": model_name,
            "target_embedding_dimension": BGE_M3_DIMENSION,
            "artifact_kind": "dry_run" if _backend_name(embedding_backend) == "fake" else "production",
            "model": _actual_model_name(embedding_backend),
            "dimension": int(embedding_backend.dimension),
            "dim": int(embedding_backend.dimension),
            "chunking_version": CHUNKING_VERSION,
            "build_version": "offline-rag-v1",
            "built_at": datetime.now(timezone.utc).isoformat(),
            "counts": {
                "documents": len(parsed_sources),
                "chunks": len(chunks),
                "unresolved": len(unresolved),
                "failed": len(failed_sources),
            },
            "source_types": sorted({parsed.document.source_type for parsed in parsed_sources}),
            "unresolved": [
                {
                    "source": parsed.document.source,
                    "source_url": parsed.document.source_url,
                    "reason": parsed.document.unresolved_reason,
                    "document_id": parsed.document_id,
                }
                for parsed in unresolved
            ],
            "failed_sources": failed_sources,
            "artifacts": {
                "faiss": index_path.name,
                "chunks": "chunks.jsonl",
                "corpus": "corpus.jsonl",
                "bm25": "bm25_index.json",
            },
        }
        _write_json(manifest_path, manifest)
        results.append(
            BuildResult(
                domain=selected_domain,
                output_dir=str(target),
                documents=len(parsed_sources),
                chunks=len(chunks),
                unresolved=len(unresolved),
                failed=len(failed_sources),
                index_path=str(index_path),
                chunks_path=str(target / "chunks.jsonl"),
                corpus_path=str(target / "corpus.jsonl"),
                manifest_path=str(manifest_path),
            )
        )
    return results


def _parse_sources(
    domain: str,
    root: Path,
    metadata: dict[str, dict[str, Any]],
    *,
    chunk_size: int,
    overlap: int,
) -> tuple[list[ParsedSource], list[dict[str, str]]]:
    if not root.exists():
        return [], []
    allowed_suffixes = {".md"} if domain == "apple_support" else {".md", ".txt", ".pdf"}
    parsed: list[ParsedSource] = []
    failed: list[dict[str, str]] = []
    for path in sorted(
        (item for item in root.rglob("*") if item.is_file() and item.suffix.lower() in allowed_suffixes),
        key=lambda item: item.relative_to(root).as_posix(),
    ):
        relative_source = path.relative_to(root).as_posix()
        sidecar = metadata.get(path.stem, {})
        try:
            document = read_source(path, domain=domain, metadata=sidecar, source=relative_source)
            parsed.append(
                chunk_document(
                    document,
                    document_id=stable_document_id(domain, relative_source),
                    chunk_size=chunk_size,
                    overlap=overlap,
                )
            )
        except Exception as exc:  # keep one bad source from hiding other domain artifacts
            failed.append({"source": relative_source, "reason": f"{type(exc).__name__}: {exc}"})
    return parsed, failed


def _load_metadata(root: Path) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    if not root.exists():
        return result
    for path in sorted(root.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        key = str(data.get("source_id") or path.stem)
        result[key] = data
    return result


def _write_chunks(path: Path, chunks: list[KnowledgeChunk]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for chunk in chunks:
            handle.write(json.dumps(chunk.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")


def _write_corpus(path: Path, chunks: list[KnowledgeChunk]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for chunk in chunks:
            frequencies = _term_frequencies(chunk["retrieval_text"])
            handle.write(
                json.dumps(
                    {
                        "chunk_id": chunk["chunk_id"],
                        "retrieval_text": chunk["retrieval_text"],
                        "terms": sorted(frequencies),
                        "term_frequencies": frequencies,
                        "document_length": sum(frequencies.values()),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            )


def _write_bm25(path: Path, chunks: list[KnowledgeChunk]) -> None:
    frequencies = [_term_frequencies(chunk["retrieval_text"]) for chunk in chunks]
    postings: dict[str, list[dict[str, int]]] = {}
    for index, term_counts in enumerate(frequencies):
        for term, frequency in sorted(term_counts.items()):
            postings.setdefault(term, []).append({"index": index, "tf": frequency})
    lengths = [sum(term_counts.values()) for term_counts in frequencies]
    index = {
        "version": "bm25-v1",
        "k1": 1.5,
        "b": 0.75,
        "chunk_ids": [chunk["chunk_id"] for chunk in chunks],
        "document_lengths": lengths,
        "term_frequencies": frequencies,
        "document_frequency": {term: len(entries) for term, entries in sorted(postings.items())},
        "average_document_length": sum(lengths) / len(lengths) if lengths else 0.0,
        "postings": postings,
    }
    _write_json(path, index)


def _write_faiss(path: Path, chunks: list[KnowledgeChunk], backend: EmbeddingBackend) -> None:
    if faiss is None:
        raise RuntimeError("faiss-cpu is required to build dense artifacts")
    index = faiss.IndexFlatIP(int(backend.dimension))
    if chunks:
        texts = [chunk["retrieval_text"] for chunk in chunks]
        if hasattr(backend, "embed_batch"):
            vectors = backend.embed_batch(texts)
        else:
            vectors = [backend.embed_text(text) for text in texts]  # type: ignore[attr-defined]
        matrix = np.vstack([np.asarray(vector, dtype=np.float32) for vector in vectors])
        if matrix.shape != (len(chunks), int(backend.dimension)):
            raise ValueError("embedding backend returned an unexpected matrix shape")
        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        matrix = np.divide(matrix, norms, out=matrix, where=norms != 0)
        index.add(matrix.astype(np.float32))
    faiss.write_index(index, str(path))


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _terms(text: str) -> list[str]:
    terms: list[str] = []
    for match in _TECH_TOKEN_RE.finditer(text):
        token = match.group(0)
        if re.fullmatch(r"[\u4e00-\u9fff]+", token):
            terms.extend(_segment_cjk(token))
        else:
            terms.append(token.lower())
    return terms


def _segment_cjk(text: str) -> list[str]:
    try:
        import jieba
    except ImportError:
        jieba = None
    if jieba is not None:
        return [token for token in jieba.lcut(text, cut_all=False) if token.strip()]
    result: list[str] = []
    cursor = 0
    while cursor < len(text):
        match = next((word for word in _CJK_WORDS if text.startswith(word, cursor)), None)
        if match:
            result.append(match)
            cursor += len(match)
        else:
            result.append(text[cursor])
            cursor += 1
    return result


def _term_frequencies(text: str) -> dict[str, int]:
    return dict(sorted(Counter(_terms(text)).items()))


def _backend_name(backend: EmbeddingBackend) -> str:
    return str(getattr(backend, "backend_name", backend.__class__.__name__.lower()))


def _actual_model_name(backend: EmbeddingBackend) -> str:
    return str(getattr(backend, "model_name", _backend_name(backend)))
