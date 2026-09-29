"""Render the saved five-case CE transition trace without model calls."""

import json
from pathlib import Path


root = Path("artifacts/rag_global_sparse_ce_transition_20260923")
rows = [json.loads(line) for line in (root / "traces.jsonl").read_text(encoding="utf-8").splitlines()]
assert len(rows) == 5 and len({row["query_id"] for row in rows}) == 5
lines = ["# Dev v4 CE 转移的逐题证据", "",
         "60 题的 A/B 已保存指标筛出 5 个 CE 退化题；这里只对它们重放 RRF20→CE20。A/B Top10 排名及指标与已保存结果逐题一致。", "",
         "| 题号 | RRF ΔRecall/MRR/nDCG | CE ΔRecall/MRR/nDCG | A-only/B-only RRF20 | A CE10 相关项丢失 |",
         "|---|---|---|---:|---|"]
for row in rows:
    fmt = lambda key: "/".join(f"{row[key][metric]:+.3f}" for metric in ("recall@10", "mrr@10", "ndcg@10"))
    lines.append(f"| {row['query_id']} | {fmt('rrf_metric_delta_B_minus_A')} | {fmt('ce_metric_delta_B_minus_A')} | {len(row['A_only_rrf20'])}/{len(row['B_only_rrf20'])} | {', '.join(row['A_ce10_relevant_lost_in_B']) or '无'} |")
lines += ["", "相同 query/chunk pair 在 A/B 两个候选池的 CE 分数最大绝对差 < 0.000001；这支持分数本身基本稳定，不能把排序变化说成 CE 对同一 pair 重算出了不同判断。", ""]
lines += ["三题 RRF 有提升而 CE 指标下降，都是相关项仍留在 CE10 但排序变差。`apple_002` 是最终 Recall 损失题：grade-1 相关片段在 A CE 第 10、B CE 第 11；B-only 的同域候选在 B CE 第 10，分数 0.1939 高于该相关片段的 0.1600，同时该片段的 RRF 位次 4→6。`agent_007` 只有 nDCG 微降，相关项 6→7。候选集合竞争得到直接支持；CE 对相同 pair 的分数变化及跨域竞争未在这些题中得到支持。未标注候选是否真正相关仍须另行 adjudicate。", ""]
for row in rows:
    lines += [f"## {row['query_id']}", "", f"问题：{row['query']}", "",
              "| qrel chunk | grade | A RRF | B RRF | A CE/score | B CE/score |",
              "|---|---:|---:|---:|---|---|"]
    for chunk_id, value in row["qrel_transitions"].items():
        rank = lambda name: str(value[f"{name}_rrf_rank"]) if value[f"{name}_rrf_rank"] is not None else "未进 RRF20"
        ce = lambda name: f"{value[f'{name}_ce_rank']} / {value[f'{name}_ce_score']:.4f}" if value[f"{name}_ce_rank"] is not None else "未进 CE20"
        lines.append(f"| {chunk_id} | {value['grade']} | {rank('A')} | {rank('B')} | {ce('A')} | {ce('B')} |")
    lines += ["", "A-only RRF20 候选（chunk/domain/source/CE rank/score）："]
    for value in row["A_only_rrf20"]:
        lines.append(f"- {value['chunk_id']} / {value['domain']} / {value['source']} / {value['ce_rank']} / {value['ce_score']:.4f}")
    if not row["A_only_rrf20"]:
        lines.append("- 无")
    lines.append("")
    lines.append("B-only RRF20 候选（chunk/domain/source/CE rank/score）：")
    for value in row["B_only_rrf20"]:
        lines.append(f"- {value['chunk_id']} / {value['domain']} / {value['source']} / {value['ce_rank']} / {value['ce_score']:.4f}")
    if not row["B_only_rrf20"]:
        lines.append("- 无")
    for chunk_id, values in row["B_unjudged_ce10_before_lost_qrel"].items():
        lines.append(f"B CE10 排在丢失 qrel `{chunk_id}` 前、且不在 qrels 的候选：")
        lines.extend(f"- {value['chunk_id']} / {value['domain']} / {value['source']} / {value['ce_rank']} / {value['ce_score']:.4f}" for value in values)
    lines.append("")
lines += ["未列入 qrels 的候选只能称为未标注，不能据此认定不相关。此报告只是转移诊断，没有测试 CE 修复、阈值或新集泛化。", ""]
(root / "mechanism.md").write_text("\n".join(lines), encoding="utf-8")
print(json.dumps({"cases": [row["query_id"] for row in rows],
                  "lost_relevant": {row["query_id"]: row["A_ce10_relevant_lost_in_B"]
                                    for row in rows if row["A_ce10_relevant_lost_in_B"]}}, ensure_ascii=False))
