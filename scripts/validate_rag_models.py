"""Validate that the required real Round 3 models can load locally."""

from __future__ import annotations

import argparse
import json
import os
from typing import Any

from rag.embeddings import BGE_M3_DIMENSION, BGE_M3_MODEL
from rag.reranker import RERANKER_MODEL


def validate(*, local_only: bool = True) -> dict[str, Any]:
    result: dict[str, Any] = {
        "embedding_model": BGE_M3_MODEL,
        "embedding_dimension": BGE_M3_DIMENSION,
        "reranker_model": RERANKER_MODEL,
        "fake_embedding": True,
        "fake_reranker": True,
        "status": "blocked",
        "errors": [],
    }
    old_offline = os.environ.get("HF_HUB_OFFLINE")
    if local_only:
        os.environ["HF_HUB_OFFLINE"] = "1"
    try:
        from sentence_transformers import CrossEncoder, SentenceTransformer

        kwargs = {"local_files_only": True} if local_only else {}
        embedding = SentenceTransformer(BGE_M3_MODEL, **kwargs)
        dimension = int(embedding.get_sentence_embedding_dimension())
        if dimension != BGE_M3_DIMENSION:
            raise ValueError(f"BGE-M3 dimension mismatch: {dimension} != {BGE_M3_DIMENSION}")
        reranker = CrossEncoder(RERANKER_MODEL, model_kwargs=kwargs or None)
        result.update(
            {
                "fake_embedding": False,
                "fake_reranker": False,
                "embedding_dimension": dimension,
                "reranker_backend": "sentence_transformers",
                "status": "ready",
            }
        )
        # One real inference proves the models are executable, not merely cached.
        embedding.encode(["Round 3 model validation"], normalize_embeddings=True)
        reranker.predict([["query", "Round 3 model validation"]])
    except Exception as exc:
        result["errors"].append(f"{type(exc).__name__}: {exc}")
    finally:
        if old_offline is None:
            os.environ.pop("HF_HUB_OFFLINE", None)
        else:
            os.environ["HF_HUB_OFFLINE"] = old_offline
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--allow-download", action="store_true")
    parser.add_argument("--output", type=str)
    args = parser.parse_args()
    result = validate(local_only=not args.allow_download)
    rendered = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
    print(rendered)
    if args.output:
        from pathlib import Path

        target = Path(args.output)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(rendered + "\n", encoding="utf-8")
    return 0 if result["status"] == "ready" else 2


if __name__ == "__main__":
    raise SystemExit(main())
