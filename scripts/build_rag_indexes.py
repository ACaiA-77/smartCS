"""Build the Round 1 domain-separated offline RAG artifacts."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rag.build import DOMAINS, build_indexes
from rag.embeddings import BGE_M3_DIMENSION, BGE_M3_MODEL, create_embedding_backend


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build apple_support and agent_engineering RAG indexes")
    parser.add_argument("--domain", choices=[*DOMAINS, "all"], default="all")
    parser.add_argument("--output-dir", default="vector_store/rag_indexes")
    parser.add_argument("--apple-sources", default="knowledge_base")
    parser.add_argument("--agent-sources", default="knowledge_sources/agent_engineering")
    parser.add_argument("--metadata-dir", default="knowledge_sources/metadata")
    parser.add_argument(
        "--embedding-backend",
        default="hash",
        choices=["fake", "hash", "local", "sentence_transformers", "bge-m3"],
        help="hash is deterministic dry-run output; local loads the configured model",
    )
    parser.add_argument("--embedding-model", default=BGE_M3_MODEL)
    parser.add_argument("--embedding-dim", type=int, default=BGE_M3_DIMENSION)
    parser.add_argument("--chunk-size", type=int, default=900)
    parser.add_argument("--overlap", type=int, default=120)
    parser.add_argument("--reset", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_arg_parser().parse_args(argv)
    backend = create_embedding_backend(
        args.embedding_backend,
        model_name=args.embedding_model,
        dimension=args.embedding_dim,
    )
    results = build_indexes(
        domain=args.domain,
        output_dir=args.output_dir,
        apple_sources=args.apple_sources,
        agent_sources=args.agent_sources,
        metadata_dir=args.metadata_dir,
        embedding_backend=backend,
        model_name=args.embedding_model,
        chunk_size=args.chunk_size,
        overlap=args.overlap,
        reset=args.reset,
    )
    print(json.dumps([result.__dict__ for result in results], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
