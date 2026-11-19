"""Independent RAG ingestion for managed and read-only Obsidian vaults.

Builds and incrementally maintains an isolated FAISS index (``OBSIDIAN_INDEX_PATH``)
using a strictly local embedding backend. Supports both the four-zone managed
workspace and an external non-four-zone read-only source vault, skips hidden
directories/symlinks/proposals, and removes chunks for deleted notes.

Usage::

    python -m scripts.ingest_obsidian_vault --vault-dir ./data/obsidian_vault \
        --source-vault-dir /path/to/obsidian-vault \
        --index-path ./vector_store/obsidian_index --reset
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from api.copilot_bootstrap import (
    assert_isolated_obsidian_index_path,
    resolve_local_obsidian_embedding_backend,
)
from knowledge_engine.vault import READ_ZONES, SOURCE_ZONE, ObsidianVault
from memory.long_term import EmbeddingBackend, LongTermMemory

_SKIPPED_PREFIXES = ("wiki/proposals/",)


@dataclass(frozen=True)
class VaultIngestResult:
    vault_dir: str
    zones: tuple[str, ...]
    index_path: str
    metadata_path: str
    loaded_count: int
    skipped_count: int
    total_documents: int
    removed_count: int = 0
    source_vault_dir: str | None = None


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_zones(
    value: str | Iterable[str] | None, *, include_source: bool = False
) -> tuple[str, ...]:
    if value is None:
        return ("raw", "notes", SOURCE_ZONE) if include_source else ("raw", "notes")
    if isinstance(value, str):
        zones = tuple(part.strip() for part in value.split(",") if part.strip())
    else:
        zones = tuple(str(part).strip() for part in value if str(part).strip())
    if not zones:
        raise ValueError("at least one vault zone is required")
    unknown = [zone for zone in zones if zone not in READ_ZONES]
    if unknown:
        allowed = ", ".join(READ_ZONES)
        raise ValueError(f"unknown vault zone(s) {unknown}; expected one of {allowed}")
    return zones


def collect_vault_markdown(
    vault_dir: str | Path | None = None,
    zones: str | Iterable[str] | None = None,
    *,
    source_vault_dir: str | Path | None = None,
) -> tuple[ObsidianVault, list[Path], tuple[str, ...]]:
    """Return the vault, its safe markdown files in the requested zones, and effective zones."""

    if vault_dir is not None and str(vault_dir).strip():
        vault = ObsidianVault(vault_dir, source_root=source_vault_dir)
        selected = _parse_zones(zones, include_source=source_vault_dir is not None)
    elif source_vault_dir is not None and str(source_vault_dir).strip():
        vault = ObsidianVault(source_vault_dir, read_only=True)
        selected = _parse_zones(zones or (SOURCE_ZONE,), include_source=True)
    else:
        raise ValueError("at least one of vault_dir or source_vault_dir is required")

    paths: list[Path] = []
    for zone in selected:
        for path in vault.list_notes(zone):
            relative = vault.relative_path(path)
            if any(relative.startswith(prefix) for prefix in _SKIPPED_PREFIXES):
                continue
            if not path.is_file():
                continue
            paths.append(path)
    paths.sort(key=lambda item: vault.relative_path(item).casefold())
    return vault, paths, selected


def _load_markdown_files(
    memory: LongTermMemory,
    vault: ObsidianVault,
    paths: list[Path],
) -> tuple[int, int, int]:
    """Incrementally load/update files and prune deleted notes."""

    active_sources = {str(path) for path in paths}
    stale_sources = {
        str(doc.get("metadata", {}).get("source_path") or "")
        for doc in memory.documents
        if str(doc.get("metadata", {}).get("source_path") or "") not in active_sources
    }
    removed_count = memory.remove_documents_by_source_paths(stale_sources)

    existing_by_source: dict[str, list[dict]] = {}
    for document in memory.documents:
        src = str(document.get("metadata", {}).get("source_path") or "")
        if src:
            existing_by_source.setdefault(src, []).append(document)

    skipped = 0
    changed_sources: list[str] = []
    pending_docs: list[dict] = []
    updated_at = _utc_now()
    backend_name = str(
        getattr(memory.embedding_backend, "backend_name", memory.embedding_backend.__class__.__name__)
    )
    model_name = str(getattr(memory.embedding_backend, "model_name", backend_name))

    for path in paths:
        try:
            content = path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError):
            continue
        source_path = str(path)
        relative = vault.relative_path(path)
        document_hash = _content_hash(content)
        existing = existing_by_source.get(source_path, [])
        if existing and all(
            document.get("metadata", {}).get("document_hash") == document_hash
            for document in existing
        ):
            skipped += 1
            continue
        if existing:
            changed_sources.append(source_path)

        chunks = memory._chunk_text(content)
        source_doc_id = memory._stable_doc_id(source_path, "document")
        zone = vault._zone_of(path)
        for chunk_index, chunk in enumerate(chunks):
            pending_docs.append(
                {
                    "content": chunk,
                    "source": path.name,
                    "metadata": {
                        "file": str(path),
                        "source_path": source_path,
                        "vault_path": relative,
                        "zone": zone,
                        "doc_id": source_doc_id,
                        "chunk_id": memory._stable_chunk_id(
                            source_doc_id, chunk, {"chunk_index": chunk_index}
                        ),
                        "content_hash": _content_hash(chunk),
                        "document_hash": document_hash,
                        "chunk_index": chunk_index,
                        "chunk_count": len(chunks),
                        "file_type": path.suffix.lower().lstrip("."),
                        "updated_at": updated_at,
                        "embedding_backend": backend_name,
                        "embedding_model": model_name,
                    },
                }
            )

    if changed_sources:
        memory.remove_documents_by_source_paths(changed_sources)

    if pending_docs:
        ordered_indices = sorted(
            range(len(pending_docs)), key=lambda idx: len(pending_docs[idx]["content"])
        )
        embedded_vectors: list[Any] = [None] * len(pending_docs)
        cursor = 0
        total_pending = len(ordered_indices)
        while cursor < total_pending:
            max_len = len(pending_docs[ordered_indices[cursor]]["content"])
            if max_len <= 600:
                step = 32
            elif max_len <= 1200:
                step = 8
            else:
                step = 2
            batch_indices = ordered_indices[cursor : cursor + step]
            # Peek tail length so a tail crossing into a larger bucket drops batch size cleanly.
            tail_len = len(pending_docs[batch_indices[-1]]["content"])
            if tail_len > 1200 and step > 2:
                batch_indices = batch_indices[:2]
            elif tail_len > 600 and step > 8:
                batch_indices = batch_indices[:8]
            batch_vectors = memory._embed_batch(
                [pending_docs[idx]["content"] for idx in batch_indices]
            )
            for idx, vec in zip(batch_indices, batch_vectors):
                embedded_vectors[idx] = vec
            cursor += len(batch_indices)
            if total_pending > 256 and (cursor == total_pending or cursor % 320 < len(batch_indices)):
                print(
                    f"[obsidian-ingest] embedded {cursor}/{total_pending} chunks",
                    file=sys.stderr,
                    flush=True,
                )
        memory.add_documents_batch(pending_docs, embeddings=embedded_vectors)

    return len(pending_docs), skipped, removed_count


def ingest_obsidian_vault(
    vault_dir: str | Path | None = None,
    *,
    source_vault_dir: str | Path | None = None,
    index_path: str | Path = "./vector_store/obsidian_index",
    zones: str | Iterable[str] | None = None,
    embedding_backend: EmbeddingBackend | str | None = None,
    embedding_model: str | None = None,
    embedding_dim: int = 1536,
    reset: bool = False,
    save: bool = True,
) -> VaultIngestResult:
    """Build or incrementally update the vault's independent local FAISS index."""

    resolved_backend = resolve_local_obsidian_embedding_backend(
        backend=embedding_backend,
        model_name=embedding_model,
        embedding_dim=embedding_dim,
    )
    index_path = assert_isolated_obsidian_index_path(index_path)
    if reset:
        for target in (index_path, index_path.with_suffix(".meta.json")):
            if target.exists():
                target.unlink()

    vault, paths, effective_zones = collect_vault_markdown(
        vault_dir, zones, source_vault_dir=source_vault_dir
    )
    memory = LongTermMemory(
        index_path=str(index_path),
        embedding_dim=int(resolved_backend.dimension),
        embedding_backend=resolved_backend,
    )
    loaded_count, skipped_count, removed_count = _load_markdown_files(memory, vault, paths)
    if save:
        memory.save()

    return VaultIngestResult(
        vault_dir=str(vault.root),
        zones=effective_zones,
        index_path=str(memory.index_path),
        metadata_path=str(memory.index_path.with_suffix(".meta.json")),
        loaded_count=loaded_count,
        skipped_count=skipped_count,
        total_documents=len(memory.documents),
        removed_count=removed_count,
        source_vault_dir=str(vault.source_root) if vault.source_root is not None else None,
    )


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build or update the independent local Obsidian vault RAG index"
    )
    parser.add_argument(
        "--vault-dir",
        default=None,
        help="Managed Obsidian vault workspace (defaults to OBSIDIAN_VAULT_PATH)",
    )
    parser.add_argument(
        "--source-vault-dir",
        default=None,
        help="Optional read-only external Obsidian vault (defaults to OBSIDIAN_SOURCE_VAULT_PATH)",
    )
    parser.add_argument(
        "--zones",
        default=None,
        help="Comma-separated vault zones (defaults to raw,notes[,source])",
    )
    parser.add_argument(
        "--index-path",
        default=None,
        help="Independent Obsidian FAISS index output path (defaults to OBSIDIAN_INDEX_PATH)",
    )
    parser.add_argument(
        "--embedding-backend",
        default=None,
        help="Local embedding backend (local|hash; remote backends are forbidden)",
    )
    parser.add_argument(
        "--embedding-model",
        default=None,
        help="Local embedding model name (defaults to EMBEDDING_MODEL)",
    )
    parser.add_argument(
        "--embedding-dim",
        type=int,
        default=int(os.getenv("EMBEDDING_DIM", "1536")),
        help="Embedding dimension for the deterministic hash fallback backend",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Remove the existing Obsidian FAISS index and metadata before ingesting",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    from dotenv import load_dotenv

    load_dotenv()
    args = build_arg_parser().parse_args(argv)
    vault_dir = args.vault_dir if args.vault_dir is not None else os.getenv("OBSIDIAN_VAULT_PATH")
    source_vault_dir = (
        args.source_vault_dir
        if args.source_vault_dir is not None
        else os.getenv("OBSIDIAN_SOURCE_VAULT_PATH")
    )
    index_path = args.index_path or os.getenv("OBSIDIAN_INDEX_PATH", "./vector_store/obsidian_index")
    result = ingest_obsidian_vault(
        vault_dir=vault_dir,
        source_vault_dir=source_vault_dir,
        index_path=index_path,
        zones=args.zones,
        embedding_backend=args.embedding_backend,
        embedding_model=args.embedding_model,
        embedding_dim=args.embedding_dim,
        reset=args.reset,
    )
    payload = asdict(result)
    payload["zones"] = list(result.zones)
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
