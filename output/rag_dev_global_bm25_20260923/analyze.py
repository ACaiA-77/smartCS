"""Dev-only counterfactual: domain-local versus one-corpus BM25 statistics."""

import hashlib
import json
import math
from collections import defaultdict
from pathlib import Path

from rag.build import _terms
from rag.evaluation.metrics import evaluate_ranking
from rag.retriever import global_ranked_candidates
from rag.sparse_retriever import SparseRetriever
from scripts.evaluate_rag_retrieval import load_queries, validate_benchmark_manifest


ROOT = Path("artifacts/rag_round3/production_indexes")
BENCHMARK = Path("benchmarks/rag")
BASELINE = Path("artifacts/rag_corrected_baseline_20260921")
OUTPUT = Path("artifacts/rag_dev_global_bm25_20260923")
DOMAINS = ("apple_support", "agent_engineering")
CUTOFFS = (1, 3, 5, 10)
# Fixed before observing the counterfactual: material @10 reduction without
# increased @1 contamination or >2-point losses in any full-Dev retrieval metric.
MIN_APPLE_WRONG_DOMAIN_10_REDUCTION = 0.10
MAX_FULL_DEV_METRIC_LOSS = 0.02


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def aggregate(rows, keys):
    return {key: sum(row[key] for row in rows) / len(rows) for key in keys}


def wrong_domain(ranking, expected):
    return {
        f"wrong_domain_rate@{cutoff}":
            sum(hit["domain"] != expected for hit in ranking[:cutoff]) / len(ranking[:cutoff])
            if ranking[:cutoff] else 0.0
        for cutoff in CUTOFFS
    }


def global_bm25(query, retrievers, global_df, global_n, global_avgdl, k1, b):
    """Score frozen postings with shared N/df/avgdl; never rebuild an index."""
    scores = defaultdict(float)
    for term in set(_terms(query)):
        df = global_df.get(term, 0)
        if not df:
            continue
        idf = math.log((global_n - df + 0.5) / (df + 0.5) + 1.0)
        for domain in DOMAINS:
            index = retrievers[domain].index
            assert len(index["postings"].get(term, [])) == index["document_frequency"].get(term, 0)
            for posting in index["postings"].get(term, []):
                position, tf = posting["index"], posting["tf"]
                assert index["term_frequencies"][position][term] == tf
                norm = 1 - b + b * index["document_lengths"][position] / global_avgdl
                scores[(domain, position)] += idf * tf * (k1 + 1) / (tf + k1 * norm)
    ranked = sorted(
        ((domain, position, score) for (domain, position), score in scores.items() if score > 0),
        key=lambda row: (-row[2], row[0], retrievers[row[0]]._chunks[row[1]]["chunk_id"]),
    )[:10]
    return [{"chunk_id": retrievers[domain]._chunks[position]["chunk_id"],
             "domain": domain, "score": score}
            for domain, position, score in ranked]


def main():
    validate_benchmark_manifest(BENCHMARK, ROOT)
    assert json.loads((BENCHMARK / "benchmark_manifest.json").read_text(encoding="utf-8")) == json.loads(
        (BASELINE / "benchmark_manifest.json").read_text(encoding="utf-8")
    )
    rows = json.loads((BASELINE / "metrics_per_query.json").read_text(encoding="utf-8"))
    assert len(rows) == 60 and len({row["query_id"] for row in rows}) == 60
    assert sorted(row["domain"] for row in rows) == [DOMAINS[1]] * 30 + [DOMAINS[0]] * 30
    assert all("scoring_mode" not in row for row in rows)
    source_queries = {query.query_id: query for query in load_queries(BENCHMARK)}
    assert len(source_queries) == len(rows)
    assert all((row["query"], row["domain"], row["kind"], row["qrels"])
               == (source_queries[row["query_id"]].query, source_queries[row["query_id"]].domain,
                   source_queries[row["query_id"]].kind, source_queries[row["query_id"]].qrels)
               for row in rows)

    retrievers = {domain: SparseRetriever(ROOT, domain=domain) for domain in DOMAINS}
    first, second = (retrievers[domain].index for domain in DOMAINS)
    assert first["k1"] == second["k1"] and first["b"] == second["b"]
    for domain in DOMAINS:
        index = retrievers[domain].index
        assert len(index["chunk_ids"]) == len(index["document_lengths"]) == len(index["term_frequencies"]) == len(retrievers[domain]._chunks)
        assert index["chunk_ids"] == [chunk["chunk_id"] for chunk in retrievers[domain]._chunks]
    global_n = sum(len(retrievers[domain]._chunks) for domain in DOMAINS)
    global_avgdl = sum(sum(retrievers[domain].index["document_lengths"]) for domain in DOMAINS) / global_n
    global_df = defaultdict(int)
    for domain in DOMAINS:
        for term, df in retrievers[domain].index["document_frequency"].items():
            global_df[term] += df

    metric_keys = ("recall@10", "mrr@10", "ndcg@10")
    wrong_keys = tuple(f"wrong_domain_rate@{cutoff}" for cutoff in CUTOFFS)
    cases = []
    for row in rows:
        current = global_ranked_candidates(
            [hit for domain in DOMAINS for hit in retrievers[domain].search(row["query"], top_k=20)],
            top_k=10, rank_field="sparse_rank",
        )
        assert [hit.chunk_id for hit in current] == row["rankings"]["bm25"]
        current_ranking = [{"chunk_id": hit.chunk_id, "domain": hit.domain, "score": hit.score} for hit in current]
        current_metrics = {**evaluate_ranking(row["rankings"]["bm25"], row["qrels"]),
                           **wrong_domain(current_ranking, row["domain"])}
        assert all(abs(current_metrics[key] - row["variants"]["bm25"][key]) < 1e-12
                   for key in (*metric_keys, *wrong_keys))
        global_ranking = global_bm25(row["query"], retrievers, global_df, global_n,
                                     global_avgdl, first["k1"], first["b"])
        global_metrics = {**evaluate_ranking([hit["chunk_id"] for hit in global_ranking], row["qrels"]),
                          **wrong_domain(global_ranking, row["domain"])}
        cases.append({"query_id": row["query_id"], "domain": row["domain"],
                      "current": {"ranking": current_ranking,
                                  "metrics": {key: current_metrics[key] for key in (*metric_keys, *wrong_keys)}},
                      "global_corpus": {"ranking": global_ranking,
                                        "metrics": {key: global_metrics[key] for key in (*metric_keys, *wrong_keys)}}})

    apple = [case for case in cases if case["domain"] == "apple_support"]
    summary = {}
    for cohort_name, cohort, keys in (("apple_30", apple, (*wrong_keys, *metric_keys)),
                                    ("full_dev_60", cases, metric_keys)):
        current = aggregate([case["current"]["metrics"] for case in cohort], keys)
        global_corpus = aggregate([case["global_corpus"]["metrics"] for case in cohort], keys)
        summary[cohort_name] = {"current": current, "global_corpus": global_corpus,
                                "delta_global_minus_current": {key: global_corpus[key] - current[key] for key in keys}}
    apple_delta = summary["apple_30"]["delta_global_minus_current"]
    full_delta = summary["full_dev_60"]["delta_global_minus_current"]
    supported = (
        apple_delta["wrong_domain_rate@10"] <= -MIN_APPLE_WRONG_DOMAIN_10_REDUCTION
        and apple_delta["wrong_domain_rate@1"] <= 0
        and all(full_delta[key] >= -MAX_FULL_DEV_METRIC_LOSS for key in metric_keys)
    )
    report = {
        "task_id": "smartcs_dev_global_bm25_counterfactual_20260923",
        "scope": "Dev v4 only; offline global BM25 statistics; no production, Dense, RRF, CE, or Holdout changes",
        "inputs_sha256": {
            "dev_baseline_per_query": sha256(BASELINE / "metrics_per_query.json"),
            **{f"{domain}_bm25": sha256(ROOT / domain / "bm25_index.json") for domain in DOMAINS},
        },
        "bm25": {"k1": first["k1"], "b": first["b"], "global_n": global_n,
                 "global_avgdl": global_avgdl, "global_vocabulary_size": len(global_df)},
        "pre_registered_rule": {
            "apple_wrong_domain_10_min_absolute_reduction": MIN_APPLE_WRONG_DOMAIN_10_REDUCTION,
            "apple_wrong_domain_1_must_not_increase": True,
            "full_dev_each_metric_max_absolute_loss": MAX_FULL_DEV_METRIC_LOSS,
            "metrics": list(metric_keys),
        },
        "mechanism_supported_by_this_dev_counterfactual": supported,
        "summary": summary,
        "cases": cases,
        "limitations": [
            "The result tests one global-corpus score counterfactual on seen Dev v4; it is not an unseen generalization result.",
            "A positive result supports but does not uniquely prove score-scale mismatch as the cause of cross-domain errors.",
            "Query terms use the current runtime tokenizer; frozen postings are reused, and historical build tokenizer is not independently established.",
            "Dense, RRF, CE, production retrieval, and Holdout v1 are unchanged and unevaluated here.",
        ],
    }
    OUTPUT.mkdir(parents=True, exist_ok=True)
    (OUTPUT / "counterfactual.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = ["# Dev v4 统一语料 BM25 反事实", "",
             "仅使用冻结的 Dev v4 queries/qrels 和 BM25 postings。A 与保存的 Dev baseline 逐题一致；B 将两个领域的 N、df、avgdl 合并后计算统一 BM25 分数。没有重建或修改索引。", "",
             f"预注册判定：Apple wrong-domain@10 至少降低 {MIN_APPLE_WRONG_DOMAIN_10_REDUCTION:.2f}（绝对值），@1 不增加，全 Dev Recall/MRR/nDCG@10 均不下降超过 {MAX_FULL_DEV_METRIC_LOSS:.2f}。", "",
             "| 范围 | 指标 | A 当前 | B 统一语料 | B−A |", "|---|---|---:|---:|---:|"]
    for cohort_name, keys in (("apple_30", (*wrong_keys, *metric_keys)), ("full_dev_60", metric_keys)):
        cohort = summary[cohort_name]
        for key in keys:
            lines.append(f"| {cohort_name} | {key} | {cohort['current'][key]:.4f} | {cohort['global_corpus'][key]:.4f} | {cohort['delta_global_minus_current'][key]:+.4f} |")
    lines += ["", f"Dev 机制判定：{'达到预注册支持条件' if supported else '未达到预注册支持条件'}。该结果不等于已经证明唯一根因，也不构成生产改动建议。", "",
              "逐题排序、分数和输入哈希见 `counterfactual.json`。历史索引构建时的 tokenizer backend 未独立证实；此实验复用冻结 postings。", ""]
    (OUTPUT / "counterfactual.md").write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps({"status": "PASS", "apple_wrong_domain_10_current": summary["apple_30"]["current"]["wrong_domain_rate@10"],
                      "apple_wrong_domain_10_global": summary["apple_30"]["global_corpus"]["wrong_domain_rate@10"],
                      "mechanism_supported": supported}, ensure_ascii=False))


if __name__ == "__main__":
    main()
