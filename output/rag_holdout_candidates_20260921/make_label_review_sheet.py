"""Render the candidate fact groups for final human review without scoring them."""

import json
from pathlib import Path


root = Path("artifacts/rag_holdout_candidates_20260921")
queries = {row["query_id"]: row["query"] for row in (
    json.loads(line) for line in (root / "proposed_queries.jsonl").read_text(encoding="utf-8").splitlines()
)}
groups = [json.loads(line) for line in (root / "qrel_equivalence_groups.jsonl").read_text(encoding="utf-8").splitlines()]
lines = [
    "# Holdout 事实组人工终审表", "",
    "题目文字 36 道已由用户审阅通过；事实组、等级和等价成员依据用户回复“合理”于 2026-09-23 记录为通过。此记录不证明 qrel 已在全语料中穷尽。", "",
    "重点：Apple 023 的截断片段单列等级 1；Apple 029 的两个等级 1 事实不可合并；Agent 010 的一条证据由原始等级 1 复核为等级 2。", "",
    "等级 2 表示直接、完整支持问题；等级 1 表示只支持部分答案。成员是可互相替代的同一答案事实，不是多个独立事实。", "",
    "| 题号与题目 | 等级 | 事实组判断依据 | 成员 chunk ID |", "|---|---:|---|---|",
]
for group in groups:
    members = "<br>".join(
        member["chunk_id"] + ("（原始 1 → 2）" if "original_relevance" in member else "")
        for member in group["members"]
    )
    lines.append(
        f"| {group['query_id']}<br>{queries[group['query_id']]} | {group['relevance']} | "
        f"{group['rationale']} | {members} |"
    )
lines += ["", f"共 {len(queries)} 题、{len(groups)} 个事实组、{sum(len(group['members']) for group in groups)} 条证据。", ""]
Path("output/rag_holdout_candidates_20260921/label_review_sheet.md").write_text("\n".join(lines), encoding="utf-8")
