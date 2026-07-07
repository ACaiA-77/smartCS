from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

from dotenv import load_dotenv

from memory.long_term import EmbeddingBackend, LongTermMemory

load_dotenv(dotenv_path=Path.cwd() / ".env")


@dataclass(frozen=True)
class IngestResult:
    kb_dir: str
    index_path: str
    metadata_path: str
    loaded_count: int
    total_documents: int


def ingest_knowledge_base(
    kb_dir: str,
    index_path: str = "./vector_store/faiss_index",
    embedding_backend: EmbeddingBackend | None = None,
    min_score: float = -1.0,
    save: bool = True,
    reset: bool = False,
) -> IngestResult:
    if reset:
        path = Path(index_path)
        for target in (path, path.with_suffix(".meta.json")):
            if target.exists():
                target.unlink()

    memory = LongTermMemory(
        index_path=index_path,
        embedding_backend=embedding_backend,
        min_score=min_score,
    )
    loaded_count = memory.load_knowledge_base(kb_dir)
    if save:
        memory.save()

    return IngestResult(
        kb_dir=kb_dir,
        index_path=str(memory.index_path),
        metadata_path=str(memory.index_path.with_suffix(".meta.json")),
        loaded_count=loaded_count,
        total_documents=len(memory.documents),
    )


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build or update the SmartCS FAISS knowledge index")
    parser.add_argument("--kb-dir", required=True, help="Directory containing .md/.txt knowledge files")
    parser.add_argument(
        "--index-path",
        default=os.getenv("FAISS_INDEX_PATH", "./vector_store/faiss_index"),
        help="FAISS index output path",
    )
    parser.add_argument(
        "--min-score",
        type=float,
        default=float(os.getenv("RAG_MIN_SCORE", "-1.0")),
        help="Retrieval score threshold stored on the LongTermMemory instance",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Remove the existing FAISS index and metadata before ingesting",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_arg_parser().parse_args(argv)
    result = ingest_knowledge_base(
        kb_dir=args.kb_dir,
        index_path=args.index_path,
        min_score=args.min_score,
        reset=args.reset,
    )
    print(json.dumps(asdict(result), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
