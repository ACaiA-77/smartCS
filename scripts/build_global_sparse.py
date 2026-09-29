"""Build the opt-in global BM25 candidate from existing domain artifacts."""

import argparse
import json
from pathlib import Path

from rag.global_sparse import build_global_sparse_artifact


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-root", type=Path, default=Path("vector_store/rag_indexes"))
    parser.add_argument("--allow-dry-run", action="store_true")
    args = parser.parse_args()
    path = build_global_sparse_artifact(args.artifact_root, allow_dry_run=args.allow_dry_run)
    print(json.dumps({"global_sparse_artifact": str(path)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
