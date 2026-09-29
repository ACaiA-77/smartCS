"""Project frozen chunk text in source order, without retrieval rankings."""

import json
from pathlib import Path


root = Path("artifacts/rag_round3/production_indexes")
lines = ["# Frozen corpus source catalog", "", "For source-first challenge authoring only; no retriever or model was called.", ""]
for domain in ("apple_support", "agent_engineering"):
    chunks = [json.loads(line) for line in (root / domain / "chunks.jsonl").read_text(encoding="utf-8").splitlines()]
    for chunk in chunks:
        if domain == "agent_engineering" and not chunk["source"].startswith("Getnotes/"):
            continue
        lines.extend([
            f"## {domain} | {chunk['source']} | {chunk['chunk_index']} | {chunk['chunk_id']}",
            " > ".join(chunk.get("heading_path") or []), "",
            chunk.get("content", "")[:900], "",
        ])
target = Path("output/rag_cross_domain_sparse_challenge_20260923/source_catalog.md")
target.write_text("\n".join(lines), encoding="utf-8")
print(str(target))
