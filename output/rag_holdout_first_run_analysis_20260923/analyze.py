"""Offline analysis of saved Holdout rankings; never calls retrieval or models."""

import hashlib
import json
from collections import Counter
from pathlib import Path

from rag.build import _terms


run = Path("artifacts/rag_holdout_v1_first_run_20260923")
gold = Path("benchmarks/rag_holdout_v1")
index = Path("artifacts/rag_round3/production_indexes")
assert hashlib.sha256((run / "metrics_per_query.json").read_bytes()).hexdigest() == (
    "8271e3af999cf0eb9f63687c579b5bfb2145fd0c88f92b2cd474e1b08d55bfb6"
)
query_rows = json.loads((run / "metrics_per_query.json").read_text(encoding="utf-8"))
groups = [json.loads(line) for line in (gold / "qrel_groups.jsonl").read_text(encoding="utf-8").splitlines()]
groups_by_query = {}
for group in groups:
    groups_by_query.setdefault(group["query_id"], []).append(group)
chunks = {}
for domain in ("apple_support", "agent_engineering"):
    for line in (index / domain / "chunks.jsonl").read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        chunks[row["chunk_id"]] = row


def hits(ranking, query_groups):
    member_to_group = {
        member: group["group_id"]
        for group in query_groups for member in group["member_chunk_ids"]
    }
    result = {}
    for rank, chunk_id in enumerate(ranking[:10], 1):
        group_id = member_to_group.get(chunk_id)
        if group_id is not None and group_id not in result:
            result[group_id] = {"rank": rank, "chunk_id": chunk_id}
    return result


cases = []
for row in query_rows:
    qid = row["query_id"]
    query_groups = groups_by_query[qid]
    group_by_id = {group["group_id"]: group for group in query_groups}
    found = {variant: hits(ranking, query_groups) for variant, ranking in row["rankings"].items()}
    dense, bm25 = found["dense"], found["bm25"]
    rrf, rerank = found["hybrid_rrf"], found["hybrid_rerank"]
    all_groups = set(group_by_id)
    dropped = sorted(set(rrf) - set(rerank))
    missing = sorted(all_groups - set(rerank))
    first_stage_gap = sorted(all_groups - set(dense) - set(bm25) - set(rrf) - set(rerank))
    rrf_recovers = sorted(gid for gid in rrf if gid not in dense or gid not in bm25)
    metrics = row["variants"]
    assert row["qrel_group_count"] == len(query_groups)
    assert all(abs(len(found[variant]) / len(query_groups) - metrics[variant]["recall@10"]) < 1e-12
               for variant in metrics)
    flags = []
    if first_stage_gap:
        flags.append("FIRST_STAGE_TOP10_WEAKNESS")
    if rrf_recovers:
        flags.append("RRF_RECOVERS")
    if (metrics["hybrid_rerank"]["mrr@10"] > metrics["hybrid_rrf"]["mrr@10"]
            or metrics["hybrid_rerank"]["ndcg@10"] > metrics["hybrid_rrf"]["ndcg@10"]):
        flags.append("RERANK_RECOVERS_ORDERING")
    if dropped:
        flags.append("RERANK_DROPS_RELEVANT_FACT")
    if any(metrics[variant]["wrong_domain_rate@10"] > 0 for variant in metrics):
        flags.append("CROSS_DOMAIN_CONTAMINATION")
    if (metrics["hybrid_rerank"]["recall@10"] == 1
            and metrics["hybrid_rerank"]["ndcg@10"] == 1
            and metrics["hybrid_rerank"]["wrong_domain_rate@10"] == 0):
        flags.append("FULL_PASS")
    def describe(group_id):
        group = group_by_id[group_id]
        return {
            "group_id": group_id, "relevance": group["relevance"],
            "rationale": group["rationale"],
            "rrf_hit": rrf.get(group_id), "rerank_hit": rerank.get(group_id),
            "dense_hit": dense.get(group_id), "bm25_hit": bm25.get(group_id),
            "missing_in_saved_top10": [variant for variant in ("dense", "bm25") if group_id not in found[variant]],
        }
    wrong_bm25 = []
    if row["domain"] == "apple_support":
        for rank, chunk_id in enumerate(row["rankings"]["bm25"][:10], 1):
            chunk = chunks[chunk_id]
            if chunk["domain"] != row["domain"]:
                wrong_bm25.append({
                    "rank": rank, "chunk_id": chunk_id, "source": chunk["source"],
                    "heading_path": chunk["heading_path"],
                    "excerpt": chunk["content"][:180],
                    "shared_query_terms": sorted(
                        set(_terms(row["query"])) & set(_terms(chunk["retrieval_text"])),
                        key=lambda term: (-len(term), term),
                    )[:12],
                })
    cases.append({
        "query_id": qid, "domain": row["domain"], "kind": row["kind"],
        "query": row["query"], "flags": flags,
        "scores_at_10": {variant: {key: values[key] for key in (
            "recall@10", "mrr@10", "ndcg@10", "wrong_domain_rate@10")}
            for variant, values in metrics.items()},
        "missing_after_rerank": [describe(gid) for gid in missing],
        "rrf_dropped_by_rerank": [describe(gid) for gid in dropped],
        "first_stage_top10_gap": [describe(gid) for gid in first_stage_gap],
        "rrf_recovered_groups": [describe(gid) for gid in rrf_recovers],
        "bm25_wrong_domain_apple": wrong_bm25,
    })

counts = Counter(flag for case in cases for flag in case["flags"])
apple_bm25_wrong_sources = Counter(
    hit["source"] for case in cases for hit in case["bm25_wrong_domain_apple"]
)
report = {
    "task_id": "smartcs_holdout_first_run_analysis_20260923",
    "source_metrics_per_query_sha256": "8271e3af999cf0eb9f63687c579b5bfb2145fd0c88f92b2cd474e1b08d55bfb6",
    "method": "offline join of saved Top10 rankings with frozen Gold fact groups and frozen chunk metadata; no new retrieval or scoring",
    "limitations": [
        "Flags overlap; counts are not a partition of 36 questions.",
        "FIRST_STAGE_TOP10_WEAKNESS means absent from saved Top10 lists; candidate ranks 11-20 were not saved, so it does not prove first-stage candidate absence.",
        "BM25 wrong-domain examples show observed chunks and excerpts; lexical-cause labels require human interpretation.",
    ],
    "query_count": len(cases), "flag_counts": dict(counts),
    "apple_bm25_wrong_sources": dict(apple_bm25_wrong_sources), "cases": cases,
}
(run / "postmortem.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

lines = [
    "# Holdout v1 首次运行：离线失败分析", "",
    "只读取已保存的 Top10 排名、冻结事实组和冻结 chunk 元数据；没有调用模型、重新检索或改动评测参数。各标签可重叠，不是 36 题的互斥分区。", "",
    "| 标签 | 题数 |", "|---|---:|",
]
for flag in ("FIRST_STAGE_TOP10_WEAKNESS", "RRF_RECOVERS", "RERANK_RECOVERS_ORDERING",
             "RERANK_DROPS_RELEVANT_FACT", "CROSS_DOMAIN_CONTAMINATION", "FULL_PASS"):
    lines.append(f"| {flag} | {counts[flag]} |")
lines += [
    "", "## 主要发现", "",
    "- RRF→rerank 的整体 Recall@10 从 94.44% 到 91.67%；具体丢失的是 Agent017 和 Agent035 各一个等级 1 的补充事实组，见下表。其他排序收益应与这两处损失分别记录。",
    "- Agent rerank 的 Recall@10 为 83.33%；未覆盖的 5 个事实组分布在摘要缓存、异步工具消息、评估 Rubric、上下文感知检索与训练阶段五道题，其中 Agent017/035 属于 rerank 后丢失。",
    "- Apple 的 BM25 Top10 共 180 个位置，其中 115 个跨域；103 个来自同一本 Agent 书。可见误召常共享泛化词或单字词；当前环境没有 `jieba`，`_terms` 会使用回退分词，而 BM25 分领域计算 IDF 后按原始分数全局合并。这两点是待在 Dev 上验证的机制假设，不据本次 Holdout 直接改参数。",
]
lines += ["", "## RRF 命中但 rerank 丢失的事实组", ""]
for case in cases:
    for group in case["rrf_dropped_by_rerank"]:
        lines.append(f"- **{case['query_id']}** {case['query']}：{group['group_id']}（等级 {group['relevance']}），RRF rank {group['rrf_hit']['rank']}；{group['rationale']}")
lines += ["", "## RRF 相对至少一路单路 Top10 找回的事实组", ""]
for case in cases:
    for group in case["rrf_recovered_groups"]:
        lines.append(f"- **{case['query_id']}** 相对 {','.join(group['missing_in_saved_top10'])}：{group['group_id']}，RRF rank {group['rrf_hit']['rank']}；{group['rationale']}")
lines += ["", "## Agent Engineering 在 rerank Top10 仍缺失的事实组", ""]
for case in cases:
    if case["domain"] == "agent_engineering":
        for group in case["missing_after_rerank"]:
            lines.append(f"- **{case['query_id']}** [{case['kind']}] {case['query']}：{group['group_id']}（等级 {group['relevance']}）；{group['rationale']}")
lines += ["", "## 四条保存的 Top10 排名均未覆盖的事实组", ""]
for case in cases:
    for group in case["first_stage_top10_gap"]:
        lines.append(f"- **{case['query_id']}**：{group['group_id']}（等级 {group['relevance']}）；{group['rationale']}")
lines += ["", "## Apple BM25 跨域误召较高的实例", ""]
apple = sorted((case for case in cases if case["domain"] == "apple_support"),
               key=lambda case: -case["scores_at_10"]["bm25"]["wrong_domain_rate@10"])
for case in apple[:8]:
    wrong = case["bm25_wrong_domain_apple"]
    if wrong:
        first = wrong[0]
        lines.append(f"- **{case['query_id']}** wrong-domain@10={len(wrong)}/10，问题：{case['query']}；首个误召 rank {first['rank']}，来源 `{first['source']}`，共同分词：{', '.join(first['shared_query_terms'])}；摘录：{first['excerpt'].replace(chr(10), ' ')[:120]}")
lines += ["", "跨域误召来源计数（Apple 的 BM25 Top10）：" + "；".join(f"`{source}` {count} 条" for source, count in apple_bm25_wrong_sources.most_common()), ""]
lines += ["", "## 解释边界", "",
          "`FIRST_STAGE_TOP10_WEAKNESS` 只表示保存的 Dense/BM25/RRF/rerank Top10 都未覆盖该事实组；没有保存候选第 11–20 位，不能据此证明首阶段完全未召回。", "",
          "跨域例子是已发生的误召；共同分词用当前 `_terms` 从已保存 query 与冻结 chunk 的 `retrieval_text` 计算，未重新检索。它们是可能的词法诱因，不是 BM25 逐项贡献的证明。Holdout v1 已见结果，后续优化应在 Dev 或新 challenge set 验证。", ""]
(run / "postmortem.md").write_text("\n".join(lines), encoding="utf-8")
print(json.dumps({"status": "PASS", "queries": len(cases), "flag_counts": dict(counts)}, ensure_ascii=False))
