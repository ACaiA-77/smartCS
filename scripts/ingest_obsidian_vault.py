"""Fallback RAG ingestion for an Obsidian vault.

The wiki/ontology path is the primary knowledge interface.  This script builds
a FAISS index over the vault's markdown so ``knowledge_search`` can still find
things the wiki compiler has not materialised yet.  It scans only the requested
zones (``raw`` and ``notes`` by default), skips ``wiki/proposals/``, and updates
the index incrementally: unchanged files are left alone, changed files replace
their old chunks.

Usage::

    python -m scripts.ingest_obsidian_vault --vault-dir ~/vault \
        --zones raw,notes --index-path ./vector_store/obsidian_index --reset
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from knowledge_engine.vault import ALLOWED_ZONES, ObsidianVault
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


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_zones(value: str | Iterable[str] | None) -> tuple[str, ...]:
    if value is None:
        return ("raw", "notes")
    if isinstance(value, str):
        zones = tuple(part.strip() for part in value.split(",") if part.strip())
    else:
        zones = tuple(str(part).strip() for part in value if str(part).strip())
    if not zones:
        raise ValueError("at least one vault zone is required")
    unknown = [zone for zone in zones if zone not in ALLOWED_ZONES]
    if unknown:
        allowed = ", ".join(ALLOWED_ZONES)
        raise ValueError(f"unknown vault zone(s) {unknown}; expected one of {allowed}")
    return zones


def collect_vault_markdown(
    vault_dir: str | Path, zones: str | Iterable[str] | None = None
) -> tuple[ObsidianVault, list[Path]]:
    """Return the vault and its markdown files in the requested zones."""

    vault = ObsidianVault(vault_dir)
    selected = _parse_zones(zones)
    paths: list[Path] = []
    for zone in selected:
        for path in vault.list_notes(zone):
            relative = path.relative_to(vault.root).as_posix()
            if any(relative.startswith(prefix) for prefix in _SKIPPED_PREFIXES):
                continue
            if not path.is_file():
                continue
            paths.append(path)
    paths.sort(key=lambda item: item.relative_to(vault.root).as_posix().casefold())
    return vault, paths


def _load_markdown_files(
    memory: LongTermMemory, vault: ObsidianVault, paths: list[Path]
) -> tuple[int, int]:
    """Load/update files using the LongTermMemory.load_knowledge_base pattern."""

    loaded = 0
    skipped = 0
    updated_at = _utc_now()
    for path in paths:
        content = path.read_text(encoding="utf-8")
        source_path = str(path)
        relative = path.relative_to(vault.root).as_posix()
        document_hash = _content_hash(content)
        existing = [
            document
            for document in memory.documents
            if document.get("metadata", {}).get("source_path") == source_path
        ]
        if existing and all(
            document.get("metadata", {}).get("document_hash") == document_hash
            for document in existing
        ):
            skipped += 1
            continue
        if existing:
            memory.remove_documents_by_source_path(source_path)

        chunks = memory._chunk_text(content)
        source_doc_id = memory._stable_doc_id(source_path, "document")
        for chunk_index, chunk in enumerate(chunks):
            memory.add_document(
                content=chunk,
                source=path.name,
                metadata={
                    "file": str(path),
                    "source_path": source_path,
                    "vault_path": relative,
                    "zone": relative.split("/", 1)[0],
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
                },
            )
            loaded += 1
    return loaded, skipped


def ingest_obsidian_vault(
    vault_dir: str | Path,
    *,
    index_path: str | Path = "./vector_store/obsidian_index",
    zones: str | Iterable[str] | None = None,
    embedding_backend: EmbeddingBackend | None = None,
    embedding_dim: int = 1536,
    reset: bool = False,
    save: bool = True,
) -> VaultIngestResult:
    """Build or incrementally update the vault's fallback FAISS index."""

    index_path = Path(index_path)
    if reset:
        for target in (index_path, index_path.with_suffix(".meta.json")):
            if target.exists():
                target.unlink()

    vault, paths = collect_vault_markdown(vault_dir, zones)
    memory = LongTermMemory(
        index_path=str(index_path),
        embedding_dim=embedding_dim,
        embedding_backend=embedding_backend,
    )
    loaded_count, skipped_count = _load_markdown_files(memory, vault, paths)
    if save:
        memory.save()

    return VaultIngestResult(
        vault_dir=str(vault.root),
        zones=_parse_zones(zones),
        index_path=str(memory.index_path),
        metadata_path=str(memory.index_path.with_suffix(".meta.json")),
        loaded_count=loaded_count,
        skipped_count=skipped_count,
        total_documents=len(memory.documents),
    )


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build or update the Obsidian vault RAG fallback index"
    )
    parser.add_argument("--vault-dir", required=True, help="Obsidian vault root")
    parser.add_argument(
        "--zones",
        default="raw,notes",
        help="Comma-separated vault zones (default: raw,notes)",
    )
    parser.add_argument(
        "--index-path",
        default=os.getenv("OBSIDIAN_INDEX_PATH", "./vector_store/obsidian_index"),
        help="FAISS index output path",
    )
    parser.add_argument(
        "--embedding-dim",
        type=int,
        default=int(os.getenv("EMBEDDING_DIM", "1536")),
        help="Embedding dimension for the deterministic fallback backend",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Remove the existing FAISS index and metadata before ingesting",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_arg_parser().parse_args(argv)
    result = ingest_obsidian_vault(
        vault_dir=args.vault_dir,
        index_path=args.index_path,
        zones=args.zones,
        embedding_dim=args.embedding_dim,
        reset=args.reset,
    )
    payload = asdict(result)
    payload["zones"] = list(result.zones)
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
