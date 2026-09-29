"""List frozen source chunks by document order for challenge authoring."""

import json
from collections import Counter
from pathlib import Path


root = Path("artifacts/rag_round3/production_indexes")
counts = Counter()
for domain in ("apple_support", "agent_engineering"):
    rows = [json.loads(line) for line in (root / domain / "chunks.jsonl").read_text(encoding="utf-8").splitlines()]
    for row in rows:
        counts[(domain, row["source"])] += 1
    for (name, source), count in sorted(counts.items()):
        if name == domain:
            print(f"{name}\t{count}\t{source}")
