"""Dev-only A/B RRF20 to CE20 transition trace; no retrieval changes."""

import hashlib
import json
import math
from pathlib import Path

from rag.embeddings import SentenceTransformerEmbeddingBackend
from rag.evaluation.metrics import evaluate_ranking
from rag.fusion import reciprocal_rank_fusion
from rag.reranker import CrossEncoderReranker
from rag.retriever import HybridRetriever, global_ranked_candidates
from scripts.evaluate_rag_retrieval import load_queries, validate_benchmark_manifest
from scripts.validate_rag_models import validate


benchmark = Path("benchmarks/rag")
index_root = Path("artifacts/rag_round3/production_indexes")
baseline = Path("artifacts/rag_corrected_baseline_20260921")
candidate = Path("artifacts/rag_global_sparse_candidate_dev_20260923")
output = Path("artifacts/rag_global_sparse_ce_transition_20260923")
read = lambda path: json.loads(path.read_text(encoding="utf-8"))
digest = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
METRICS = ("recall@10", "mrr@10", "ndcg@10")


def stage(retriever, query, dense):
    sparse = global_ranked_candidates(retriever.sparse_search(query, top_k=20),
                                      top_k=20, rank_field="sparse_rank")
    rrf = reciprocal_rank_fusion(dense, sparse, rrf_k=60, top_k=20)
    rrf_rank = {hit.chunk_id: rank for rank, hit in enumerate(rrf, 1)}
    ce = retriever.rerank(query, rrf, top_k=20)
    assert len(ce) == len(rrf) and {hit.chunk_id for hit in ce} == set(rrf_rank)
    return {
        "sparse20": [hit.chunk_id for hit in sparse],
        "rrf20": [hit.chunk_id for hit in rrf],
        "ce20": [hit.chunk_id for hit in ce],
        "candidates": {hit.chunk_id: {
            "chunk_id": hit.chunk_id, "domain": hit.domain, "source": hit.source,
            "heading_path": hit.heading_path, "rrf_rank": rrf_rank[hit.chunk_id],
            "ce_rank": rank, "ce_score": hit.rerank_score,
        } for rank, hit in enumerate(ce, 1)},
    }


def main():
    validate_benchmark_manifest(benchmark, index_root)
    assert read(benchmark / "benchmark_manifest.json") == read(baseline / "benchmark_manifest.json")
    queries = load_queries(benchmark)
    a_saved = {row["query_id"]: row for row in read(baseline / "metrics_per_query.json")}
    b_saved = {row["query_id"]: row for row in read(candidate / "metrics_per_query.json")}
    assert len(queries) == len(a_saved) == len(b_saved) == 60
    focus = []
    ce_loss_only = []
    for item in queries:
        old, new = a_saved[item.query_id], b_saved[item.query_id]
        assert (old["query"], old["domain"], old["qrels"]) == (new["query"], new["domain"], new["qrels"])
        rrf_gain = any(new["variants"]["hybrid_rrf"][key] > old["variants"]["hybrid_rrf"][key] + 1e-12 for key in METRICS)
        ce_loss = any(new["variants"]["hybrid_rerank"][key] < old["variants"]["hybrid_rerank"][key] - 1e-12 for key in METRICS)
        if rrf_gain and ce_loss:
            focus.append(item.query_id)
        elif ce_loss:
            ce_loss_only.append(item.query_id)
    assert focus == ["apple_010", "apple_011", "apple_027"]
    assert ce_loss_only == ["apple_002", "agent_007"]
    detailed = set(focus + ce_loss_only)
    validation = validate(local_only=True)
    if validation.get("status") != "ready" or validation.get("fake_embedding") or validation.get("fake_reranker"):
        raise RuntimeError("real local embedding/reranker models are required")
    backend, reranker = SentenceTransformerEmbeddingBackend(), CrossEncoderReranker()
    arms = {name: HybridRetriever(index_root, embedding_backend=backend, reranker=reranker,
                                  sparse_mode=mode)
            for name, mode in (("A", "domain_local_v1"), ("B", "global_corpus_v1"))}
    output.mkdir(parents=True, exist_ok=True)
    trace_path = output / "traces.jsonl"
    traces = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()] if trace_path.exists() else []
    assert len({row["query_id"] for row in traces}) == len(traces)
    assert all(row["query_id"] in detailed for row in traces)
    completed = {row["query_id"] for row in traces}
    with trace_path.open("a", encoding="utf-8") as stream:
        for item in queries:
            if item.query_id not in detailed or item.query_id in completed:
                continue
            dense = global_ranked_candidates(arms["A"].dense_search(item.query, top_k=20),
                                             top_k=20, rank_field="dense_rank")
            assert [hit.chunk_id for hit in dense[:10]] == a_saved[item.query_id]["rankings"]["dense"]
            pair = {name: stage(retriever, item.query, dense) for name, retriever in arms.items()}
            for name, saved in (("A", a_saved), ("B", b_saved)):
                frozen = saved[item.query_id]
                assert (item.query, item.domain, item.kind, item.qrels) == (
                    frozen["query"], frozen["domain"], frozen["kind"], frozen["qrels"])
                assert pair[name]["sparse20"][:10] == frozen["rankings"]["bm25"]
                assert pair[name]["rrf20"][:10] == frozen["rankings"]["hybrid_rrf"]
                assert pair[name]["ce20"][:10] == frozen["rankings"]["hybrid_rerank"]
                pair[name]["metrics"] = {
                    step: evaluate_ranking(pair[name][f"{step}20"][:10], item.qrels)
                    for step, variant in (("rrf", "hybrid_rrf"), ("ce", "hybrid_rerank"))
                }
                for step, variant in (("rrf", "hybrid_rrf"), ("ce", "hybrid_rerank")):
                    assert all(math.isclose(pair[name]["metrics"][step][key],
                                            frozen["variants"][variant][key], abs_tol=1e-12)
                               for key in METRICS)
            a_ids, b_ids = set(pair["A"]["rrf20"]), set(pair["B"]["rrf20"])
            shared = a_ids & b_ids
            shared_score_deltas = {chunk_id: pair["B"]["candidates"][chunk_id]["ce_score"]
                                   - pair["A"]["candidates"][chunk_id]["ce_score"]
                                   for chunk_id in shared}
            rrf_delta = {key: pair["B"]["metrics"]["rrf"][key]
                         - pair["A"]["metrics"]["rrf"][key] for key in METRICS}
            ce_delta = {key: pair["B"]["metrics"]["ce"][key]
                        - pair["A"]["metrics"]["ce"][key] for key in METRICS}
            lost = [chunk_id for chunk_id in item.qrels
                    if chunk_id in pair["A"]["ce20"][:10] and chunk_id not in pair["B"]["ce20"][:10]]
            qrel_transitions = {chunk_id: {
                "grade": grade,
                **{f"{name}_{field}": pair[name]["candidates"].get(chunk_id, {}).get(field)
                   for name in ("A", "B") for field in ("rrf_rank", "ce_rank", "ce_score")},
            } for chunk_id, grade in item.qrels.items()}
            displacers = {chunk_id: [value for value in pair["B"]["candidates"].values()
                                     if value["ce_rank"] <= 10 and value["chunk_id"] not in item.qrels
                                     and (pair["B"]["candidates"].get(chunk_id, {}).get("ce_rank") is None
                                          or value["ce_rank"] < pair["B"]["candidates"][chunk_id]["ce_rank"])]
                          for chunk_id in lost}
            row = {
                "query_id": item.query_id, "query": item.query, "domain": item.domain,
                "kind": item.kind, "qrels": item.qrels, "arms": pair,
                "rrf_metric_delta_B_minus_A": rrf_delta, "ce_metric_delta_B_minus_A": ce_delta,
                "rrf_improves_any_metric": any(value > 1e-12 for value in rrf_delta.values()),
                "ce_worsens_any_metric": any(value < -1e-12 for value in ce_delta.values()),
                "rrf_recall_improves_ce_recall_worsens": rrf_delta["recall@10"] > 1e-12 and ce_delta["recall@10"] < -1e-12,
                "A_only_rrf20": [pair["A"]["candidates"][chunk_id] for chunk_id in pair["A"]["rrf20"] if chunk_id not in b_ids],
                "B_only_rrf20": [pair["B"]["candidates"][chunk_id] for chunk_id in pair["B"]["rrf20"] if chunk_id not in a_ids],
                "qrel_transitions": qrel_transitions, "A_ce10_relevant_lost_in_B": lost,
                "B_unjudged_ce10_before_lost_qrel": displacers,
                "shared_ce_score_max_abs_delta": max((abs(value) for value in shared_score_deltas.values()), default=0.0),
            }
            traces.append(row)
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
            stream.flush()
            print(f"{len(traces)}/{len(detailed)} {item.query_id}: A/B stage parity PASS", flush=True)
    assert len(traces) == len(detailed)
    traces.sort(key=lambda row: row["query_id"])
    flags = {
        "rrf_any_gain_ce_any_loss": [row["query_id"] for row in traces if row["rrf_improves_any_metric"] and row["ce_worsens_any_metric"]],
        "rrf_recall_gain_ce_recall_loss": [row["query_id"] for row in traces if row["rrf_recall_improves_ce_recall_worsens"]],
        "A_ce10_relevant_lost_in_B": [row["query_id"] for row in traces if row["A_ce10_relevant_lost_in_B"]],
    }
    summary = {
        "task_id": "smartcs_global_sparse_ce_transition_diagnosis_20260923",
        "scope": "Dev v4 only; existing A/B modes and real local models; no code/parameter changes or Holdout access",
        "inputs_sha256": {"A_baseline": digest(baseline / "metrics_per_query.json"),
                          "B_candidate": digest(candidate / "metrics_per_query.json"),
                          "global_sparse_manifest": digest(index_root / "global_sparse" / "manifest.json")},
        "classified_query_count": len(queries), "traced_query_count": len(traces),
        "rrf_gain_ce_loss_query_ids": focus, "additional_ce_loss_query_ids": ce_loss_only,
        "flags": flags,
        "max_shared_pair_ce_score_abs_delta": max(row["shared_ce_score_max_abs_delta"] for row in traces),
        "limitations": [
            "A missing qrel label is unjudged, not proven nonrelevant.",
            "Candidate membership/rank transitions and CE scores support hypotheses; no counterfactual CE policy was tested.",
            "Dev v4 is seen and cannot establish generalization.",
        ],
    }
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = ["# Dev v4 global sparse A/B 的 RRF20→CE20 转移", "",
             "先用已保存 A/B 结果分类全部 60 条 Dev；对 3 个 RRF 提升而 CE 下降的题目，以及另外 2 个 CE 下降题目重放 RRF20→CE20。重放 Top10 与各自保存结果一致；使用真实本地 BGE 与 CE，未改变检索逻辑。", "",
             f"RRF 任一指标提高而 CE 任一指标下降：{len(flags['rrf_any_gain_ce_any_loss'])} 题：{', '.join(flags['rrf_any_gain_ce_any_loss']) or '无'}。", "",
             f"RRF Recall 提高而 CE Recall 下降：{len(flags['rrf_recall_gain_ce_recall_loss'])} 题：{', '.join(flags['rrf_recall_gain_ce_recall_loss']) or '无'}。", "",
             f"A CE10 相关 chunk 在 B CE10 丢失：{len(flags['A_ce10_relevant_lost_in_B'])} 题：{', '.join(flags['A_ce10_relevant_lost_in_B']) or '无'}。", "",
             f"共同 query/chunk pair 的最大 CE 分数绝对差：{summary['max_shared_pair_ce_score_abs_delta']:.8f}。", "",
             "逐题 qrel 等级与 RRF/CE rank、score、A-only/B-only RRF20 候选、B CE10 中排在丢失 qrel 前的未标注候选，见 `traces.jsonl`。未标注不等于已证非相关。", ""]
    (output / "summary.md").write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps({"status": "PASS", **{key: len(value) for key, value in flags.items()},
                      "max_shared_pair_ce_score_abs_delta": summary["max_shared_pair_ce_score_abs_delta"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
