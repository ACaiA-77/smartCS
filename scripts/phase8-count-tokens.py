"""Phase 8 §8.3 — count what each captured request costs, with the project's tokenizer.

    python scripts/phase8-count-tokens.py .runtime/phase8/subagent-eval.json

Uses tiktoken `cl100k_base`, the same encoding the Python context manager
budgets with, so the numbers are comparable with the runtime's own accounting.
"""

from __future__ import annotations

import json
import sys

import tiktoken

ENCODING = tiktoken.get_encoding("cl100k_base")


def tokens(text: str) -> int:
    return len(ENCODING.encode(text or ""))


def main() -> None:
    payload = json.loads(open(sys.argv[1], encoding="utf-8").read())
    rows = []
    for capture in payload["captures"]:
        tool_tokens = sum(tokens(tool["schema"]) for tool in capture["tools"])
        rows.append({
            "config": capture["config"],
            "scenario": capture["scenario"],
            "tools": len(capture["tools"]),
            "system_prompt": tokens(capture["systemPrompt"]),
            "tool_declarations": tool_tokens,
            "user_message": tokens(capture["userMessage"]),
            "snapshot": tokens(capture["snapshot"]),
            "per_request_total": tokens(capture["systemPrompt"]) + tool_tokens + tokens(capture["userMessage"]),
            "tool_names": ",".join(tool["name"] for tool in capture["tools"]),
        })

    width = max(len(row["config"]) for row in rows)
    header = f"{'config':<{width}} {'scenario':<11} {'#tools':>6} {'prompt':>7} {'tools':>6} {'user':>5} {'total':>6}"
    print(header)
    print("-" * len(header))
    for row in rows:
        print(f"{row['config']:<{width}} {row['scenario']:<11} {row['tools']:>6} "
              f"{row['system_prompt']:>7} {row['tool_declarations']:>6} {row['user_message']:>5} "
              f"{row['per_request_total']:>6}")
    print()
    print(json.dumps(rows, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
