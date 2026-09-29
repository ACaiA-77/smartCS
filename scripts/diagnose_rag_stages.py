"""Collect a fixed, baseline-checked Agent-domain stage trace without tuning."""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import math
import os
from pathlib import Path

from rag.evaluation.evaluator import _wrong_domain_metrics
from rag.evaluation.metrics import aggregate_metrics, evaluate_ranking
from rag.evaluation.report import write_json
from rag.fusion import reciprocal_rank_fusion
from rag.retriever import global_ranked_candidates
from scripts.evaluate_rag_retrieval import load_queries, validate_benchmark_manifest

STAGES = ("FIRST_STAGE_MISS", "FUSION_DROP", "RERANK_DROP", "ORDERING_ONLY", "PASS")


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def search_pair(retriever, query: str, *, domain=None, depth=20):
    return {
        name: global_ranked_candidates(
            method(query, domains=domain, top_k=depth), top_k=depth, rank_field=field,
        )
        for name, method, field in (
            ("dense", retriever.dense_search, "dense_rank"),
            ("bm25", retriever.sparse_search, "sparse_rank"),
        )
    }


def collect_candidates(retriever, item):
    pair = search_pair(retriever, item.query)
    union = reciprocal_rank_fusion(pair["dense"], pair["bm25"], rrf_k=60)
    # Snapshot before rerank: it mutates hit.rank and hit.score in place.
    candidates = {hit.chunk_id: {
        "chunk_id": hit.chunk_id, "domain": hit.domain, "source": hit.source,
        "heading_path": hit.heading_path, "dense_rank": hit.dense_rank,
        "dense_score": hit.dense_score, "sparse_rank": hit.sparse_rank,
        "sparse_score": hit.sparse_score, "rrf_rank": rank,
        "rrf_score": hit.rrf_score, "rerank_rank": None, "rerank_score": None,
    } for rank, hit in enumerate(union, 1)}
    rrf_ids = [hit.chunk_id for hit in union]
    reranked = retriever.rerank(item.query, union[:20], top_k=20)
    if {hit.chunk_id for hit in reranked} != set(rrf_ids[:20]) or len(reranked) != len(rrf_ids[:20]):
        raise ValueError("reranker did not return every RRF20 candidate exactly once")
    for rank, hit in enumerate(reranked, 1):
        candidates[hit.chunk_id].update(rerank_rank=rank, rerank_score=hit.rerank_score)
    rankings = {name: [hit.chunk_id for hit in hits[:10]] for name, hits in pair.items()}
    rankings.update(hybrid_rrf=rrf_ids[:10], hybrid_rerank=[hit.chunk_id for hit in reranked[:10]])
    by_id = {hit.chunk_id: hit for hit in union}
    metrics = {}
    for name, ids in rankings.items():
        metrics[name] = evaluate_ranking(ids, item.qrels)
        metrics[name].update(_wrong_domain_metrics([by_id[key] for key in ids], item.domain))
    return {
        "query_id": item.query_id, "query": item.query, "domain": item.domain,
        "kind": item.kind, "qrels": item.qrels, "rankings": rankings, "variants": metrics,
        "dense20": [hit.chunk_id for hit in pair["dense"]],
        "bm25_20": [hit.chunk_id for hit in pair["bm25"]],
        "rrf_union": rrf_ids, "rrf20": rrf_ids[:20],
        "ce20": [hit.chunk_id for hit in reranked], "candidates": list(candidates.values()),
    }


def check_parity(row, baseline):
    for field in ("query_id", "query", "domain", "kind", "qrels", "rankings"):
        if row[field] != baseline[field]:
            raise ValueError(f"baseline parity mismatch: {row['query_id']} {field}")


def validate_agent_inputs(queries, baseline_rows):
    agent_rows = [row for row in baseline_rows if row["domain"] == "agent_engineering"]
    baseline = {row["query_id"]: row for row in agent_rows}
    if (len(queries) != 30 or len({item.query_id for item in queries}) != 30
            or sum(len(item.qrels) for item in queries) != 55
            or len(agent_rows) != 30 or len(baseline) != 30
            or sum(len(row["qrels"]) for row in agent_rows) != 55
            or {item.query_id for item in queries} != set(baseline)):
        raise ValueError("expected matching 30 Agent queries / 55 qrels")
    for item in queries:
        if any(getattr(item, key) != baseline[item.query_id][key]
               for key in ("query", "domain", "kind", "qrels")):
            raise ValueError(f"frozen input parity mismatch: {item.query_id}")
    return baseline


def classify(row):
    union = set(row["dense20"]) | set(row["bm25_20"])
    fused = set(row["rrf20"])
    final = set(row["ce20"][:10])
    stages = {
        key: ("FIRST_STAGE_MISS" if key not in union else
              "FUSION_DROP" if key not in fused else
              "RERANK_DROP" if key not in final else "RETAINED")
        for key in row["qrels"]
    }
    bucket = next((stage for stage in STAGES[:3] if stage in stages.values()), None)
    if bucket is None:
        metrics = evaluate_ranking(row["ce20"][:10], row["qrels"])
        bucket = "PASS" if all(math.isclose(metrics[f"{key}@10"], 1, abs_tol=1e-12)
                               for key in ("recall", "mrr", "ndcg")) else "ORDERING_ONLY"
    return {"query_id": row["query_id"], "query": row["query"], "kind": row["kind"],
            "bucket": bucket, "qrel_stages": stages}


def probe_first_stage(retriever, row, diagnosis):
    missing = [key for key, stage in diagnosis["qrel_stages"].items() if stage == "FIRST_STAGE_MISS"]
    if not missing:
        return None
    evidence = {}
    for label, domain, depth in (("oracle20", row["domain"], 20),
                                  ("global40", None, 40), ("oracle40", row["domain"], 40)):
        evidence[label] = {name: [hit.to_dict() for hit in hits]
                           for name, hits in search_pair(retriever, row["query"], domain=domain, depth=depth).items()}
    ids = {label: {hit["chunk_id"] for hits in pair.values() for hit in hits}
           for label, pair in evidence.items()}
    return {"query_id": row["query_id"], "evidence": evidence, "qrels": {key: {
        "subreason": "GLOBAL_COMPETITION_DROP" if key in ids["oracle20"] else "LOCAL_RETRIEVAL_MISS",
        "depth40": ("RECOVERED_GLOBAL_40" if key in ids["global40"] else
                    "RECOVERED_ORACLE_40_ONLY" if key in ids["oracle40"] else "NOT_RECOVERED_AT_40"),
    } for key in missing}}


def summarize(diagnoses, probes):
    def counts(rows):
        return {stage: sum(row["bucket"] == stage for row in rows) for stage in STAGES}
    return {
        "query_count": len(diagnoses),
        "qrel_count": sum(len(row["qrel_stages"]) for row in diagnoses),
        "query_counts": counts(diagnoses),
        "qrel_counts": dict(Counter(stage for row in diagnoses for stage in row["qrel_stages"].values())),
        "by_kind": {kind: counts([row for row in diagnoses if row["kind"] == kind])
                    for kind in ("semantic", "lexical", "confusing")},
        "details": {stage: [row for row in diagnoses if row["bucket"] == stage] for stage in STAGES},
        "depth40_recovery_counts": dict(Counter(value["depth40"] for probe in probes for value in probe["qrels"].values())),
    }


def protected_files(benchmark_root, baseline_root):
    return {
        "frozen": [benchmark_root / name for name in ("queries.jsonl", "qrels.jsonl", "benchmark_manifest.json")]
        + [Path(name) for name in ("scripts/build_rag_benchmark.py", "tests/test_rag_benchmark.py",
                                   "scripts/report_rag_round3.py", "tests/test_rag_report.py")],
        "corrected_baseline": [baseline_root / name for name in (
            "metrics.json", "metrics_by_domain.json", "metrics_per_query.json", "benchmark_manifest.json")],
        "historical_v3": [Path("artifacts/rag_round3") / name for name in (
            "metrics.json", "metrics_by_domain.json", "metrics_per_query.json", "model_validation.json",
            "query_overlap_report.json", "failure_analysis.md")],
    }


def hashes(paths):
    return {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}


def run(args):
    output = args.output_root.resolve()
    for protected in (args.benchmark_root, args.baseline_root, Path("artifacts/rag_round3"), args.artifact_root):
        protected = protected.resolve()
        if output == protected or output in protected.parents or protected in output.parents:
            raise ValueError("output overlaps protected inputs")
    if any((output / name).exists() for name in ("candidate_evidence.jsonl", "stage_diagnostics.json", "baseline_parity.json")):
        raise ValueError("diagnostic output already exists; refusing overwrite")
    if os.environ.get("HF_HUB_OFFLINE") != "1" or os.environ.get("TRANSFORMERS_OFFLINE") != "1":
        raise ValueError("HF_HUB_OFFLINE=1 and TRANSFORMERS_OFFLINE=1 required")
    manifest = read_json(args.benchmark_root / "benchmark_manifest.json")
    if manifest["benchmark_version"] != "rag-round3-v4-qrel-audited":
        raise ValueError("expected frozen v4 benchmark")
    validate_benchmark_manifest(args.benchmark_root, args.artifact_root)
    if manifest != read_json(args.baseline_root / "benchmark_manifest.json"):
        raise ValueError("baseline manifest mismatch")
    queries = [item for item in load_queries(args.benchmark_root) if item.domain == args.domain]
    baseline = validate_agent_inputs(queries, read_json(args.baseline_root / "metrics_per_query.json"))
    groups = protected_files(args.benchmark_root, args.baseline_root)
    before = {name: hashes(paths) for name, paths in groups.items()}
    for name, value in before.items():
        write_json(output / f"{name}_hashes_before.json", value)
    write_json(output / "benchmark_manifest.json", manifest)
    try:
        from scripts.validate_rag_models import validate
        from rag.embeddings import SentenceTransformerEmbeddingBackend
        from rag.reranker import CrossEncoderReranker
        from rag.retriever import HybridRetriever

        validation = validate(local_only=True)
        write_json(output / "model_validation.json", validation)
        expected = {"status": "ready", "fake_embedding": False, "fake_reranker": False,
                    "embedding_model": "BAAI/bge-m3", "embedding_dimension": 1024,
                    "reranker_model": "BAAI/bge-reranker-v2-m3", "errors": []}
        if any(validation.get(key) != value for key, value in expected.items()):
            raise ValueError("real cached-model validation failed")
        retriever = HybridRetriever(args.artifact_root, embedding_backend=SentenceTransformerEmbeddingBackend(),
                                    reranker=CrossEncoderReranker(), allow_dry_run=False)
        rows = []
        with (output / "candidate_evidence.jsonl").open("x", encoding="utf-8") as stream:
            for item in queries:
                row = collect_candidates(retriever, item)
                stream.write(json.dumps(row, ensure_ascii=False) + "\n")
                stream.flush()
                check_parity(row, baseline[item.query_id])
                rows.append(row)
                print(f"{len(rows)}/30 {item.query_id}: four-variant Top10 parity PASS", flush=True)
        metrics = {name: aggregate_metrics(row["variants"][name] for row in rows) for name in rows[0]["variants"]}
        expected_metrics = read_json(args.baseline_root / "metrics_by_domain.json")[args.domain]
        if set(metrics) != set(expected_metrics) or any(
            set(values) != set(expected_metrics[name]) or any(
                not math.isclose(value, expected_metrics[name][key], rel_tol=0, abs_tol=1e-12)
                for key, value in values.items()) for name, values in metrics.items()
        ):
            raise ValueError("aggregate baseline parity mismatch")
        write_json(output / "baseline_parity.json", {"status": "PASS", "query_count": len(rows),
                   "ranking_comparisons": len(rows) * 4, "metrics": metrics, "tolerance": 1e-12})
        chunks = {row["chunk_id"]: row for row in map(json.loads,
                  (args.artifact_root / args.domain / "chunks.jsonl").read_text(encoding="utf-8").splitlines())}
        diagnoses, probes = [], []
        for row in rows:
            diagnosis = classify(row)
            diagnosis["qrel_evidence"] = {key: {"relevance": grade, "source": chunks[key]["source"],
                "heading_path": chunks[key]["heading_path"], "content_excerpt": chunks[key]["content"][:600]}
                for key, grade in row["qrels"].items()}
            diagnoses.append(diagnosis)
            probe = probe_first_stage(retriever, row, diagnosis)
            if probe:
                probes.append(probe)
        for row in diagnoses:
            if row["query_id"] in {"agent_002", "agent_006", "agent_021", "agent_023"} and row["bucket"] != "RERANK_DROP":
                raise ValueError("known rerank regression invariant failed")
        summary = summarize(diagnoses, probes)
        write_json(output / "stage_diagnostics.json", diagnoses)
        write_json(output / "stage_summary.json", summary)
        if probes:
            write_json(output / "depth40_probe.json", probes)
        text = "# Agent stage diagnosis\n\nBaseline parity PASS: 30 queries / 55 qrels.\n\n"
        text += "Qrel RETAINED means it survives CE Top10; ORDERING_ONLY and PASS are query-level labels.\n\n"
        text += "```json\n" + json.dumps({key: value for key, value in summary.items() if key != "details"}, indent=2) + "\n```\n"
        for stage, details in summary["details"].items():
            text += f"\n## {stage}\n\n" + "\n".join(f"- {row['query_id']}: {row['query']}" for row in details) + "\n"
        (output / "stage_summary.md").write_text(text, encoding="utf-8")
        print(json.dumps({"status": "PASS", "query_counts": summary["query_counts"], "qrel_counts": summary["qrel_counts"]}), flush=True)
    except Exception as exc:
        write_json(output / "blocked.json", {"status": "BLOCKED", "reason": str(exc)})
        raise
    finally:
        for name, paths in groups.items():
            after = hashes(paths)
            write_json(output / f"{name}_hashes_after.json", after)
            if after != before[name]:
                raise ValueError(f"protected files changed: {name}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--benchmark-root", type=Path, default=Path("benchmarks/rag"))
    parser.add_argument("--artifact-root", type=Path, default=Path("artifacts/rag_round3/production_indexes"))
    parser.add_argument("--baseline-root", type=Path, default=Path("artifacts/rag_corrected_baseline_20260921"))
    parser.add_argument("--domain", choices=["agent_engineering"], default="agent_engineering")
    parser.add_argument("--candidate-k", type=int, choices=[20], default=20)
    parser.add_argument("--final-k", type=int, choices=[10], default=10)
    parser.add_argument("--probe-k", type=int, choices=[40], default=40)
    parser.add_argument("--output-root", type=Path, default=Path("artifacts/rag_stage_diagnosis_20260921"))
    run(parser.parse_args())


if __name__ == "__main__":
    main()
