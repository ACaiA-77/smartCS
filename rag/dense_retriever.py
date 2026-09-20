"""Strict loading and FAISS retrieval for Round 1 domain artifacts."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from .embeddings import FakeEmbeddingBackend
from .models import RetrievalHit

try:
    import faiss
except ImportError:  # pragma: no cover - requirements include faiss-cpu
    faiss = None


class ArtifactValidationError(ValueError):
    """Raised when a dense/sparse artifact set cannot be trusted."""


@dataclass(frozen=True)
class DomainArtifacts:
    domain: str
    directory: Path
    manifest: dict[str, Any]
    chunks: list[dict[str, Any]]
    bm25: dict[str, Any]
    index: Any


def _json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ArtifactValidationError(f"missing artifact: {path.name}") from exc
    except json.JSONDecodeError as exc:
        raise ArtifactValidationError(f"invalid JSON artifact: {path.name}") from exc


def _jsonl(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError as exc:
        raise ArtifactValidationError(f"missing artifact: {path.name}") from exc
    result = []
    for line_number, line in enumerate(lines, start=1):
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ArtifactValidationError(f"invalid {path.name} line {line_number}") from exc
        if not isinstance(item, dict):
            raise ArtifactValidationError(f"invalid {path.name} line {line_number}: expected object")
        result.append(item)
    return result


def _resolve_directory(path: str | Path, domain: str | None) -> tuple[Path, str]:
    root = Path(path)
    if domain and (root / domain).is_dir():
        root = root / domain
    elif domain is None and (root / "manifest.json").exists():
        pass
    elif domain and root.name != domain and not (root / "manifest.json").exists():
        root = root / domain
    if domain is None:
        manifest_path = root / "manifest.json"
        manifest = _json(manifest_path)
        domain = str(manifest.get("domain") or "")
    if not domain:
        raise ArtifactValidationError("artifact domain is missing")
    return root, domain


def load_domain_artifacts(
    path: str | Path,
    *,
    domain: str | None = None,
    allow_dry_run: bool = False,
) -> DomainArtifacts:
    """Load and cross-check every Round 1 artifact before serving results."""

    directory, resolved_domain = _resolve_directory(path, domain)
    manifest = _json(directory / "manifest.json")
    if manifest.get("domain") != resolved_domain:
        raise ArtifactValidationError(
            f"manifest domain {manifest.get('domain')!r} does not match {resolved_domain!r}"
        )
    artifact_kind = manifest.get("artifact_kind")
    if artifact_kind not in {"production", "dry_run"}:
        raise ArtifactValidationError("manifest artifact_kind must be production or dry_run")
    if artifact_kind == "dry_run" and not allow_dry_run:
        raise ArtifactValidationError(
            f"dry-run artifact rejected for {resolved_domain}; set allow_dry_run=True explicitly"
        )
    chunks = _jsonl(directory / "chunks.jsonl")
    counts = manifest.get("counts") or {}
    expected_count = counts.get("chunks")
    if expected_count != len(chunks):
        raise ArtifactValidationError(
            f"chunk count mismatch: manifest={expected_count}, chunks={len(chunks)}"
        )
    chunk_ids = []
    for chunk in chunks:
        if chunk.get("domain") != resolved_domain:
            raise ArtifactValidationError("chunk domain does not match manifest domain")
        chunk_id = str(chunk.get("chunk_id") or "")
        if not chunk_id or chunk_id in chunk_ids:
            raise ArtifactValidationError("chunk_id is missing or duplicated")
        chunk_ids.append(chunk_id)

    bm25 = _json(directory / "bm25_index.json")
    if bm25.get("chunk_ids") != chunk_ids:
        raise ArtifactValidationError("BM25 chunk_ids do not match chunks.jsonl order")
    if len(bm25.get("document_lengths", [])) != len(chunks):
        raise ArtifactValidationError("BM25 document length count does not match chunks")
    if len(bm25.get("term_frequencies", [])) != len(chunks):
        raise ArtifactValidationError("BM25 term frequency count does not match chunks")
    postings = bm25.get("postings")
    document_frequency = bm25.get("document_frequency")
    if not isinstance(postings, dict) or not isinstance(document_frequency, dict):
        raise ArtifactValidationError("BM25 postings/document_frequency are required")
    if set(postings) != set(document_frequency):
        raise ArtifactValidationError("BM25 postings and document_frequency terms disagree")
    for term, entries in postings.items():
        if document_frequency.get(term) != len(entries):
            raise ArtifactValidationError(f"BM25 document frequency mismatch for {term!r}")
        for entry in entries:
            index = entry.get("index") if isinstance(entry, dict) else None
            tf = entry.get("tf") if isinstance(entry, dict) else None
            if not isinstance(index, int) or index < 0 or index >= len(chunks):
                raise ArtifactValidationError(f"BM25 posting index out of range for {term!r}")
            if int((bm25["term_frequencies"][index] or {}).get(term, 0)) != int(tf or 0):
                raise ArtifactValidationError(f"BM25 posting TF mismatch for {term!r}")

    if faiss is None:
        raise ArtifactValidationError("faiss-cpu is required to load dense artifacts")
    try:
        index = faiss.read_index(str(directory / "index.faiss"))
    except (FileNotFoundError, RuntimeError, OSError) as exc:
        raise ArtifactValidationError(f"unable to load index.faiss: {exc}") from exc

    actual_dimension = manifest.get("actual_embedding_dimension")
    if actual_dimension is None:
        actual_dimension = manifest.get("embedding_dimension", manifest.get("dimension"))
    if not isinstance(actual_dimension, int) or actual_dimension < 1:
        raise ArtifactValidationError("manifest embedding dimension is missing or invalid")
    if int(index.d) != actual_dimension:
        raise ArtifactValidationError(
            f"FAISS dimension {index.d} does not match manifest dimension {actual_dimension}"
        )
    if int(index.ntotal) != len(chunks):
        raise ArtifactValidationError(
            f"FAISS ntotal {index.ntotal} does not match chunks {len(chunks)}"
        )

    actual_backend = manifest.get("actual_embedding_backend")
    actual_model = manifest.get("actual_embedding_model")
    if not actual_backend or not actual_model:
        raise ArtifactValidationError("manifest actual embedding backend/model are required")
    if artifact_kind == "production" and str(actual_backend).lower() in {"fake", "hash", "dry_run"}:
        raise ArtifactValidationError("fake/hash embedding artifacts cannot be marked production")
    for alias in ("embedding_backend",):
        if manifest.get(alias) not in {None, actual_backend}:
            raise ArtifactValidationError(f"manifest {alias} disagrees with actual backend")
    for alias in ("embedding_model", "model"):
        if manifest.get(alias) not in {None, actual_model}:
            raise ArtifactValidationError(f"manifest {alias} disagrees with actual model")
    for alias in ("embedding_dimension", "dimension", "dim"):
        if manifest.get(alias) not in {None, actual_dimension}:
            raise ArtifactValidationError(f"manifest {alias} disagrees with actual dimension")

    return DomainArtifacts(
        domain=resolved_domain,
        directory=directory,
        manifest=manifest,
        chunks=chunks,
        bm25=bm25,
        index=index,
    )


def _backend_name(backend: Any) -> str:
    value = getattr(backend, "backend_name", None)
    if value:
        return str(value)
    name = backend.__class__.__name__.lower()
    if "hash" in name:
        return "hash"
    if "sentence" in name:
        return "sentence_transformers"
    return name


def _model_name(backend: Any) -> str:
    return str(getattr(backend, "model_name", _backend_name(backend)))


class DenseRetriever:
    """FAISS inner-product retrieval with artifact/backend compatibility checks."""

    def __init__(
        self,
        artifact_path: str | Path,
        *,
        domain: str | None = None,
        embedding_backend: Any | None = None,
        allow_dry_run: bool = False,
    ) -> None:
        self.artifacts = load_domain_artifacts(
            artifact_path, domain=domain, allow_dry_run=allow_dry_run
        )
        actual = self.artifacts.manifest
        actual_backend = str(actual["actual_embedding_backend"])
        actual_model = str(actual["actual_embedding_model"])
        dimension = int(actual["actual_embedding_dimension"])
        if embedding_backend is None:
            if actual_backend != "fake":
                raise ArtifactValidationError(
                    "production dense retrieval requires an injected matching embedding backend"
                )
            embedding_backend = FakeEmbeddingBackend(dimension)
        if int(getattr(embedding_backend, "dimension", -1)) != dimension:
            raise ArtifactValidationError("query embedding dimension does not match artifact")
        if _backend_name(embedding_backend) != actual_backend:
            raise ArtifactValidationError(
                f"embedding backend mismatch: { _backend_name(embedding_backend)!r} != {actual_backend!r}"
            )
        if _model_name(embedding_backend) != actual_model:
            raise ArtifactValidationError(
                f"embedding model mismatch: {_model_name(embedding_backend)!r} != {actual_model!r}"
            )
        self.embedding_backend = embedding_backend

    @property
    def domain(self) -> str:
        return self.artifacts.domain

    def search(self, query: str, top_k: int = 5) -> list[RetrievalHit]:
        if not str(query).strip() or top_k <= 0 or not self.artifacts.chunks:
            return []
        backend = self.embedding_backend
        if hasattr(backend, "embed_text"):
            vector = backend.embed_text(query)
        else:
            vector = backend.embed_batch([query])[0]
        vector = np.asarray(vector, dtype=np.float32)
        norm = float(np.linalg.norm(vector))
        if norm:
            vector = vector / norm
        scores, indices = self.artifacts.index.search(vector.reshape(1, -1), min(top_k, len(self.artifacts.chunks)))
        results: list[RetrievalHit] = []
        for rank, (score, index) in enumerate(zip(scores[0], indices[0]), start=1):
            if int(index) < 0:
                continue
            chunk = self.artifacts.chunks[int(index)]
            metadata = dict(chunk)
            results.append(
                RetrievalHit(
                    chunk_id=str(chunk["chunk_id"]),
                    domain=self.domain,
                    rank=rank,
                    score=float(score),
                    source=str(chunk.get("source", "")),
                    content=str(chunk.get("content", "")),
                    retrieval_text=str(chunk.get("retrieval_text") or chunk.get("content", "")),
                    heading_path=list(chunk.get("heading_path") or []),
                    metadata=metadata,
                    dense_rank=rank,
                    dense_score=float(score),
                )
            )
        return results


__all__ = [
    "ArtifactValidationError",
    "DenseRetriever",
    "DomainArtifacts",
    "load_domain_artifacts",
]
