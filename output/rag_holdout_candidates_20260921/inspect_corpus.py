"""Read only frozen corpus, never retrieval results; reproducible annotation inspection."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.stdout.reconfigure(encoding='utf-8')
domain = sys.argv[1]
rows = [json.loads(line) for line in (ROOT / 'artifacts/rag_round3/production_indexes' / domain / 'chunks.jsonl').read_text(encoding='utf-8').splitlines()]
if len(sys.argv) > 2 and sys.argv[2] == 'overview':
    for i in range(100, len(rows), 20):
        print(i, rows[i]['content'][:300].replace('\n', ' '))
elif len(sys.argv) > 2 and sys.argv[2] == 'sources':
    from collections import defaultdict
    groups = defaultdict(list)
    for i, row in enumerate(rows):
        groups[row['source']].append(i)
    for source, indices in groups.items():
        print(min(indices), max(indices), source)
elif len(sys.argv) == 2:
    for i, row in enumerate(rows):
        print(i, row['chunk_id'], row['source'], ' > '.join(row['heading_path']))
else:
    for i in map(int, sys.argv[2].split(',')):
        row = rows[i]
        print(f"\n[{i}] {row['chunk_id']} {row['source']} {' > '.join(row['heading_path'])}\n{row['content']}")
