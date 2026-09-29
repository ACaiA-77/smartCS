"""Diagnose saved Dev v4 rerank traces and Dev-only BM25 score scales."""

import hashlib
import importlib.util
import json
import statistics
from pathlib import Path

from rag.build import _terms
from rag.retriever import global_ranked_candidates
from rag.sparse_retriever import SparseRetriever
from scripts.evaluate_rag_retrieval import validate_benchmark_manifest


benchmark = Path("benchmarks/rag")
baseline = Path("artifacts/rag_corrected_baseline_20260921")
trace_root = Path("artifacts/rag_stage_diagnosis_20260921")
index = Path("artifacts/rag_round3/production_indexes")
output = Path("artifacts/rag_dev_mechanism_20260923")
read = lambda path: json.loads(path.read_text(encoding="utf-8"))
digest = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()

validate_benchmark_manifest(benchmark, index)
assert read(benchmark / "benchmark_manifest.json") == read(baseline / "benchmark_manifest.json")
assert read(trace_root / "baseline_parity.json")["status"] == "PASS"
baseline_rows = read(baseline / "metrics_per_query.json")
baseline_by_id = {row["query_id"]: row for row in baseline_rows}
assert len(baseline_by_id) == 60
apple_rows = [row for row in baseline_rows if row["domain"] == "apple_support"]
assert len(apple_rows) == 30
apple_search = SparseRetriever(index, domain="apple_support")
agent_search = SparseRetriever(index, domain="agent_engineering")

apple_cases = []
for row in apple_rows:
    pair = {
        "apple_support": apple_search.search(row["query"], top_k=20),
        "agent_engineering": agent_search.search(row["query"], top_k=20),
    }
    merged = global_ranked_candidates(
        pair["apple_support"] + pair["agent_engineering"], top_k=10, rank_field="sparse_rank"
    )
    assert [hit.chunk_id for hit in merged] == row["rankings"]["bm25"]
    tokens = _terms(row["query"])
    wrong = [hit for hit in merged if hit.domain != "apple_support"]
    assert abs(len(wrong) / len(merged) - row["variants"]["bm25"]["wrong_domain_rate@10"]) < 1e-12
    apple_top = pair["apple_support"][0].score
    agent_top = pair["agent_engineering"][0].score
    apple_cases.append({
        "query_id": row["query_id"], "query": row["query"], "kind": row["kind"],
        "token_count": len(tokens), "single_char_token_count": sum(len(term) == 1 for term in tokens),
        "tokenizer_backend": "jieba" if importlib.util.find_spec("jieba") else "fallback_cjk",
        "apple_local_top1_score": apple_top, "agent_local_top1_score": agent_top,
        "agent_top1_exceeds_apple_top1": agent_top > apple_top,
        "wrong_domain_top10": len(wrong),
        "global_top10": [{"rank": rank, "chunk_id": hit.chunk_id, "domain": hit.domain,
                          "score": hit.score, "source": hit.source}
                         for rank, hit in enumerate(merged, 1)],
    })

agent_trace = [json.loads(line) for line in (trace_root / "candidate_evidence.jsonl").read_text(encoding="utf-8").splitlines()]
assert len(agent_trace) == 30 and len({row["query_id"] for row in agent_trace}) == 30
drops = []
for row in agent_trace:
    frozen = baseline_by_id[row["query_id"]]
    assert row["domain"] == "agent_engineering"
    assert all(row[key] == frozen[key] for key in ("query", "domain", "kind", "qrels", "rankings"))
    candidates = {candidate["chunk_id"]: candidate for candidate in row["candidates"]}
    assert set(row["rrf20"]) == set(row["ce20"])
    for chunk_id, grade in row["qrels"].items():
        if chunk_id not in row["rrf20"] or chunk_id in row["ce20"][:10]:
            continue
        evidence = candidates[chunk_id]
        ce_rank = evidence["rerank_rank"]
        neighbors = [candidate for candidate in row["candidates"]
                     if candidate["rerank_rank"] is not None
                     and max(9, ce_rank - 1) <= candidate["rerank_rank"] <= min(20, ce_rank + 1)
                     and candidate["chunk_id"] not in row["qrels"]]
        neighbors.sort(key=lambda candidate: candidate["rerank_rank"])
        hypotheses = []
        if ce_rank <= 12:
            hypotheses.append("near_top10_cutline")
        if grade == 1:
            hypotheses.append("partial_support_grade1")
        if any(candidate["source"] == evidence["source"] for candidate in neighbors):
            hypotheses.append("same_source_competitor")
        if ce_rank > 12:
            hypotheses.append("ce_semantic_preference_possible")
        drops.append({
            "query_id": row["query_id"], "query": row["query"], "kind": row["kind"],
            "chunk_id": chunk_id, "relevance": grade,
            "rrf_rank": evidence["rrf_rank"], "rrf_score": evidence["rrf_score"],
            "ce_rank": ce_rank, "ce_score": evidence["rerank_score"],
            "source": evidence["source"], "heading_path": evidence["heading_path"],
            "nearby_nonrelevant": [{"chunk_id": candidate["chunk_id"],
                                    "ce_rank": candidate["rerank_rank"],
                                    "ce_score": candidate["rerank_score"],
                                    "source": candidate["source"]} for candidate in neighbors],
            "hypotheses_not_conclusions": hypotheses,
        })

assert len(drops) == 4
summary = {
    "task_id": "smartcs_dev_retrieval_hypothesis_validation_20260923",
    "scope": "Dev v4 only; no Holdout reads, model inference, Cross-Encoder calls, or retrieval changes",
    "inputs_sha256": {
        "dev_baseline_per_query": digest(baseline / "metrics_per_query.json"),
        "agent_candidate_trace": digest(trace_root / "candidate_evidence.jsonl"),
    },
    "tokenizer_backend_runtime": apple_cases[0]["tokenizer_backend"],
    "apple_dev_query_count": len(apple_cases),
    "apple_query_single_char_token_fraction": (
        sum(case["single_char_token_count"] for case in apple_cases)
        / sum(case["token_count"] for case in apple_cases)
    ),
    "apple_bm25_wrong_domain_top10_slots": sum(case["wrong_domain_top10"] for case in apple_cases),
    "apple_bm25_agent_top1_exceeds_apple_count": sum(case["agent_top1_exceeds_apple_top1"] for case in apple_cases),
    "apple_local_top1_median": statistics.median(case["apple_local_top1_score"] for case in apple_cases),
    "agent_local_top1_median_on_apple_queries": statistics.median(case["agent_local_top1_score"] for case in apple_cases),
    "agent_dev_rerank_drop_count": len(drops),
    "agent_dev_rerank_drop_query_ids": [drop["query_id"] for drop in drops],
    "apple_cases": apple_cases, "agent_rerank_drops": drops,
    "limitations": [
        "Score-scale comparison is observational; domain-local IDF and raw global score merge are code facts, not a proven sole cause of cross-domain errors.",
        "Tokenizer backend is observed at this diagnostic runtime; historical index-build backend is not independently proven.",
        "Rerank explanations are hypotheses based on saved CE20 trace; no model counterfactual or threshold sweep was run.",
    ],
}
output.mkdir(parents=True, exist_ok=False)
(output / "diagnosis.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
lines = [
    "# Dev v4 检索机制诊断", "",
    "仅在 Dev v4 上重现 BM25 稀疏排序，并读取既有 Agent CE20 证据；没有运行 BGE/Cross-Encoder、没有调参或接触 Holdout v1。", "",
    f"Apple Dev 30 题当前 tokenizer backend：`{summary['tokenizer_backend_runtime']}`；query token 单字比例：{summary['apple_query_single_char_token_fraction']:.3f}。",
    f"Apple BM25 Top10 共 {summary['apple_bm25_wrong_domain_top10_slots']}/300 个跨域位置；Agent 领域 local Top1 分数在 {summary['apple_bm25_agent_top1_exceeds_apple_count']}/30 题高于 Apple local Top1。Apple/Agent local Top1 中位数分别为 {summary['apple_local_top1_median']:.3f}/{summary['agent_local_top1_median_on_apple_queries']:.3f}。全部 30 题的合并 Top10 与冻结 Dev baseline 逐条一致。", "",
    "实现机制：`SparseRetriever` 在各领域语料上分别计算 IDF，`HybridRetriever.sparse_search` 再按原始 score 合并。当前统计与跨域分数尺度问题一致，但不是单独的因果证明。", "",
    "## Agent Dev 的 RRF20→CE Top10 丢失", "",
    "| 题号 | qrel 等级 | RRF rank | CE rank | CE score | 诊断线索 |", "|---|---:|---:|---:|---:|---|",
]
for drop in drops:
    lines.append(f"| {drop['query_id']} | {drop['relevance']} | {drop['rrf_rank']} | {drop['ce_rank']} | {drop['ce_score']:.4f} | {', '.join(drop['hypotheses_not_conclusions'])} |")
lines += ["", "每条被挤出 qrel 附近的 non-relevant candidates、原始 CE/RRF 分数及来源见 `diagnosis.json`。上述线索只用于提出 Dev 假设；没有根据 Holdout v1 选择阈值。", "",
          "Dev v4 同时覆盖 Apple BM25 跨域和 Agent rerank 掉召回这两类形态，因此本阶段无需从 Holdout v1 复制题目建立 challenge set。", ""]
(output / "diagnosis.md").write_text("\n".join(lines), encoding="utf-8")
print(json.dumps({key: summary[key] for key in (
    "tokenizer_backend_runtime", "apple_dev_query_count", "apple_bm25_wrong_domain_top10_slots",
    "apple_bm25_agent_top1_exceeds_apple_count", "agent_dev_rerank_drop_count",
    "agent_dev_rerank_drop_query_ids",
)}, ensure_ascii=False))
