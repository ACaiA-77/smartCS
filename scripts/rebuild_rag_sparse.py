"""Safely rebuild only RAG sparse artifacts with the installed jieba tokenizer.

The command copies existing domain chunks/dense FAISS artifacts byte-for-byte,
recomputes corpus.jsonl and bm25_index.json from chunks.jsonl retrieval_text,
and writes tokenizer provenance into copied manifests.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from rag.build import DOMAINS, _write_bm25, _write_corpus
from rag.dense_retriever import ArtifactValidationError, load_domain_artifacts
from rag.global_sparse import build_global_sparse_artifact


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _tree_hashes(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): _sha256(path)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _load_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ArtifactValidationError(f"expected object JSON: {path}")
    return data


def _load_chunks(path: Path) -> list[dict[str, Any]]:
    chunks: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ArtifactValidationError(f"invalid chunks.jsonl line {line_number}: {path}") from exc
        if not isinstance(item, dict):
            raise ArtifactValidationError(f"invalid chunks.jsonl line {line_number}: expected object")
        chunks.append(item)
    return chunks


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _jieba_provenance() -> dict[str, Any]:
    try:
        import jieba
    except ImportError as exc:  # pragma: no cover - exercised through CLI environment when missing
        raise RuntimeError("jieba is required to rebuild sparse artifacts") from exc

    # Force dictionary initialization so get_dict_file() points at the active dictionary.
    jieba.lcut("智能客服检索")
    dictionary_file = jieba.get_dict_file()
    dictionary_path = Path(getattr(dictionary_file, "name", "")) if dictionary_file else None
    dictionary_sha256 = _sha256(dictionary_path) if dictionary_path and dictionary_path.is_file() else None
    return {
        "tokenizer": "jieba.lcut(cut_all=False) via rag.build._terms",
        "jieba_version": getattr(jieba, "__version__", "unknown"),
        "dictionary_path": str(dictionary_path) if dictionary_path else None,
        "dictionary_sha256": dictionary_sha256,
        "built_at": datetime.now(timezone.utc).isoformat(),
    }


def rebuild_sparse_artifacts(
    input_root: str | Path,
    output_root: str | Path,
    *,
    allow_dry_run: bool = False,
    rebuild_global_sparse: bool = True,
) -> dict[str, Any]:
    """Copy dense/chunk artifacts and rebuild only sparse files into a fresh root."""

    source_root = Path(input_root)
    target_root = Path(output_root)
    if not source_root.is_dir():
        raise FileNotFoundError(f"input root does not exist or is not a directory: {source_root}")
    if target_root.exists():
        raise FileExistsError(f"output root already exists: {target_root}")

    provenance = _jieba_provenance()
    before_hashes = _tree_hashes(source_root)
    source_artifacts = {
        domain: load_domain_artifacts(source_root, domain=domain, allow_dry_run=allow_dry_run)
        for domain in DOMAINS
    }

    parent = target_root.parent
    parent.mkdir(parents=True, exist_ok=True)
    temp_root = Path(tempfile.mkdtemp(prefix=f".{target_root.name}.tmp-", dir=parent))
    try:
        results: dict[str, Any] = {"domains": {}, "global_sparse": None}
        for domain, artifacts in source_artifacts.items():
            source_dir = artifacts.directory
            target_dir = temp_root / domain
            target_dir.mkdir(parents=True, exist_ok=False)

            for name in ("chunks.jsonl", "index.faiss"):
                shutil.copyfile(source_dir / name, target_dir / name)
                if (source_dir / name).read_bytes() != (target_dir / name).read_bytes():
                    raise ArtifactValidationError(f"copied bytes differ for {domain}/{name}")

            chunks = _load_chunks(target_dir / "chunks.jsonl")
            if [chunk.get("chunk_id") for chunk in chunks] != [chunk.get("chunk_id") for chunk in artifacts.chunks]:
                raise ArtifactValidationError(f"chunk count/order changed while copying {domain}")
            _write_corpus(target_dir / "corpus.jsonl", chunks)  # type: ignore[arg-type]
            _write_bm25(target_dir / "bm25_index.json", chunks)  # type: ignore[arg-type]

            manifest = _load_json(source_dir / "manifest.json")
            manifest["sparse_tokenizer_provenance"] = provenance
            manifest.setdefault("artifacts", {})["corpus"] = "corpus.jsonl"
            manifest.setdefault("artifacts", {})["bm25"] = "bm25_index.json"
            _write_json(target_dir / "manifest.json", manifest)

            reloaded = load_domain_artifacts(temp_root, domain=domain, allow_dry_run=allow_dry_run)
            if [chunk.get("chunk_id") for chunk in reloaded.chunks] != [chunk.get("chunk_id") for chunk in artifacts.chunks]:
                raise ArtifactValidationError(f"rebuilt artifact chunk order invalid for {domain}")
            if _sha256(source_dir / "chunks.jsonl") != _sha256(target_dir / "chunks.jsonl"):
                raise ArtifactValidationError(f"chunks hash changed for {domain}")
            if _sha256(source_dir / "index.faiss") != _sha256(target_dir / "index.faiss"):
                raise ArtifactValidationError(f"FAISS hash changed for {domain}")

            results["domains"][domain] = {
                "path": str(target_root / domain),
                "chunks": len(chunks),
                "chunks_sha256": _sha256(target_dir / "chunks.jsonl"),
                "index_sha256": _sha256(target_dir / "index.faiss"),
                "corpus_sha256": _sha256(target_dir / "corpus.jsonl"),
                "bm25_sha256": _sha256(target_dir / "bm25_index.json"),
            }

        if rebuild_global_sparse and (source_root / "global_sparse").is_dir():
            global_dir = build_global_sparse_artifact(temp_root, allow_dry_run=allow_dry_run)
            global_manifest_path = global_dir / "manifest.json"
            global_manifest = _load_json(global_manifest_path)
            global_manifest["sparse_tokenizer_provenance"] = provenance
            _write_json(global_manifest_path, global_manifest)
            results["global_sparse"] = {
                "path": str(target_root / "global_sparse"),
                "documents": len(json.loads((global_dir / "bm25_index.json").read_text(encoding="utf-8"))["chunk_ids"]),
                "bm25_sha256": _sha256(global_dir / "bm25_index.json"),
                "manifest_sha256": _sha256(global_manifest_path),
            }

        after_hashes = _tree_hashes(source_root)
        if before_hashes != after_hashes:
            raise ArtifactValidationError("input root mutated during sparse rebuild")

        temp_root.replace(target_root)
        results["output_root"] = str(target_root)
        results["tokenizer"] = provenance
        results["total_chunks"] = sum(item["chunks"] for item in results["domains"].values())
        return results
    except Exception:
        shutil.rmtree(temp_root, ignore_errors=True)
        raise


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Rebuild sparse-only RAG artifacts with jieba into a fresh output root.")
    parser.add_argument(
        "--input-root",
        default="artifacts/rag_round3/production_indexes",
        help="Existing artifact root containing domain directories.",
    )
    parser.add_argument(
        "--output-root",
        required=True,
        help="Fresh output artifact root to create; command fails if it already exists.",
    )
    parser.add_argument("--allow-dry-run", action="store_true", help="Allow dry_run fixture artifacts for tests.")
    parser.add_argument("--skip-global-sparse", action="store_true", help="Do not rebuild global_sparse even when source has it.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = rebuild_sparse_artifacts(
            args.input_root,
            args.output_root,
            allow_dry_run=args.allow_dry_run,
            rebuild_global_sparse=not args.skip_global_sparse,
        )
    except Exception as exc:
        print(f"rebuild_rag_sparse failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
