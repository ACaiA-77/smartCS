"""Validate raw benchmark evidence and compute comparisons without an LLM."""
from __future__ import annotations

import hashlib
import json
import statistics
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from rag.evaluation.metrics import aggregate_metrics, evaluate_ranking
from scripts.benchmark_reranker_devices import (
    compare_score_rows,
    latency_summary,
    load_inputs,
    rank_scores,
)

MODES = ("cpu_fp32_torch211", "gpu_fp32", "gpu_fp16", "cpu_fp32_torch213_sample")


def load(name: str) -> dict:
    return json.loads((OUT / f"{name}.json").read_text(encoding="utf-8"))


def sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def comparisons(reference: list[dict], candidate: list[dict]) -> dict:
    rows = compare_score_rows(reference, candidate)
    left, right = ({r["query_id"]: r for r in values} for values in (reference, candidate))
    for row in rows:
        a, b = left[row["query_id"]], right[row["query_id"]]
        row["top3_set_match"] = set(a["order"][:3]) == set(b["order"][:3])
        row["top10_set_match"] = set(a["order"][:10]) == set(b["order"][:10])
        row["quality_metrics_match"] = a["metrics"] == b["metrics"]
    return {
        "queries": len(rows),
        "spearman_min": min(r["spearman"] for r in rows),
        "spearman_mean": statistics.fmean(r["spearman"] for r in rows),
        "max_abs_score_delta": max(r["max_abs_score_delta"] for r in rows),
        "matching_counts": {
            key: sum(r[key] for r in rows)
            for key in ("top1_match", "top3_match", "top3_set_match", "top10_set_match",
                        "order_match", "scores_exactly_equal", "quality_metrics_match")
        },
        "changed_full_order_queries": [r["query_id"] for r in rows if not r["order_match"]],
        "changed_top3_order_queries": [r["query_id"] for r in rows if not r["top3_match"]],
        "rows": rows,
    }


def main() -> None:
    inputs, input_sha, snapshot = load_inputs(OUT / "inputs.json")
    frozen = {row["query_id"]: row for row in inputs["batches"]}
    reports = {name: load(name) for name in MODES}
    validations = {}
    for name, report in reports.items():
        assert report["metadata"]["input_sha256"] == input_sha
        assert report["metadata"]["model_snapshot"] == snapshot
        assert report["settings"]["predict_batch_size"] == 9
        assert report["settings"]["max_chars"] == 768
        assert report["metadata"]["offline"] == {"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"}
        assert report["metadata"]["parameter_count"] == 567755777
        assert report["metadata"]["torch_threads"] == 8
        assert report["metadata"]["verified_dtype"] == report["settings"]["dtype"]
        if name.startswith("gpu"):
            assert report["metadata"]["parameter_devices"] == {"cuda:0": 393}
            assert report["metadata"]["tf32"]["cuda_matmul_allow_tf32"] is False
            assert report["metadata"]["tf32"]["cudnn_allow_tf32"] is False
            assert report["metadata"]["peak_allocated_bytes"] <= report["metadata"]["peak_reserved_bytes"]
        else:
            assert report["metadata"]["parameter_devices"] == {"cpu": 393}
        timing = report["timing"]["rows"]
        assert len(timing) == report["settings"]["timing_queries"] * report["settings"]["timing_repeats"]
        assert report["timing"]["latency_ms"] == latency_summary([row["ms"] for row in timing])
        quality = report["quality"]["rows"]
        assert len(quality) == (12 if name.endswith("sample") else 60)
        for row in timing + quality:
            original = frozen[row["query_id"]]
            ids = [item["chunk_id"] for item in original["candidates"][:row["pairs"]]]
            assert row["candidate_ids"] == ids
            assert set(row["scored"]) == set(ids)
            candidates = [{"chunk_id": cid} for cid in ids]
            assert row["order"] == rank_scores(candidates, [row["scored"][cid] for cid in ids])
        for row in quality:
            assert row["qrels"] == frozen[row["query_id"]]["qrels"]
            assert row["metrics"] == evaluate_ranking(row["order"], row["qrels"], cutoffs=(10,))
        assert report["quality"]["overall"] == aggregate_metrics(row["metrics"] for row in quality)
        validations[name] = "passed: frozen IDs/qrels, rankings, metrics, latency statistics, dtype/device/offline metadata"
    cpu, gpu32, gpu16, old = (reports[name] for name in MODES)
    cpu_git = (OUT / "cpu_torch_build.txt").read_text().splitlines()[0].split("=", 1)[1]
    gpu_git = (OUT / "gpu_torch_build.txt").read_text().splitlines()[0].split("=", 1)[1]
    assert cpu_git == gpu_git
    result = {
        "schema_version": 1,
        "status": "complete_offline_one_machine_evaluation_not_production_acceptance",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "input_sha256": input_sha,
        "torch211_source_commit_cpu_and_gpu": cpu_git,
        "validations": validations,
        "devices": {},
        "torch213_sample_vs_torch211_same_first_12": comparisons(cpu["quality"]["rows"][:12], old["quality"]["rows"]),
        "torch211_first_round_latency_ms": latency_summary([r["ms"] for r in cpu["timing"]["rows"][:12]]),
        "torch213_sample_latency_ms": old["timing"]["latency_ms"],
        "limitations": [
            "Historical tuning/regression set, not independent holdout; 60 quality queries and 24 timing observations.",
            "Input preparation used a single GPU FP32 BGE-M3; no embedding model runs during rerank measurements.",
            "Timing uses the first 9 of a real RRF top20 freeze, not a guarantee of identical production top9 retrieval.",
            "Sequential runs on a shared laptop under host memory pressure; no fixed thermal/power or exclusive-resource control.",
            "Initial CUDA-build CPU run was aborted after a Windows process-snapshot WinError1455; final CPU reference uses CPU-only build of identical torch2.11 source release.",
            "GPU resource observer originally measured only the venv launcher RSS; use its host RAM samples and PyTorch GPU peaks, not root RSS as model process memory.",
            "GPU FP16 score/rank changes exist; metric equality on this set is not model numerical equivalence or unchanged generated answers.",
            "No LLM, live knowledge-answer end-to-end latency, concurrency, production GPU deployment, or production P95 was evaluated.",
        ],
    }
    for name, report in (("gpu_fp32", gpu32), ("gpu_fp16", gpu16)):
        comparison = comparisons(cpu["quality"]["rows"], report["quality"]["rows"])
        reference, candidate = cpu["quality"]["overall"], report["quality"]["overall"]
        recall_drop = reference["recall@10"] - candidate["recall@10"]
        relative_drops = {key: (reference[key] - candidate[key]) / reference[key] for key in ("mrr@10", "ndcg@10")}
        checks = {
            "rerank_p50_le_2000ms": report["timing"]["latency_ms"]["p50"] <= 2000,
            "recall_drop_le_1_percentage_point": recall_drop <= .01 + 1e-12,
            "mrr_relative_drop_le_2_percent": relative_drops["mrr@10"] <= .02 + 1e-12,
            "ndcg_relative_drop_le_2_percent": relative_drops["ndcg@10"] <= .02 + 1e-12,
            "all_quality_query_spearman_ge_095": comparison["spearman_min"] >= .95,
        }
        result["devices"][name] = {
            "latency_ms": report["timing"]["latency_ms"],
            "speedup_vs_torch211_cpu": {key: cpu["timing"]["latency_ms"][key] / report["timing"]["latency_ms"][key] for key in ("p50", "p95")},
            "quality": candidate,
            "quality_drops": {"recall_absolute": recall_drop, **relative_drops},
            "comparison": comparison,
            "experimental_gates": {"all_passed": all(checks.values()), "checks": checks},
            "peak_gpu_allocated_gib": report["metadata"]["peak_allocated_bytes"] / 1024**3,
            "peak_gpu_reserved_gib": report["metadata"]["peak_reserved_bytes"] / 1024**3,
        }
    query_rows = [json.loads(line) for line in (ROOT / "benchmarks/rag/queries.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    query_kinds = {row["query_id"]: row["kind"] for row in query_rows}
    result["timing_query_kinds"] = dict(Counter(query_kinds[row["query_id"]] for row in inputs["batches"][:12]))
    paths = [OUT / f"{name}.json" for name in MODES]
    paths += [ROOT / "scripts/benchmark_reranker_devices.py", ROOT / "tests/test_reranker_device_benchmark.py", OUT / "inputs.json"]
    result["artifact_sha256"] = {str(path.relative_to(ROOT)).replace("\\", "/"): sha(path) for path in paths}
    result["reranker_weight_sha256_at_review"] = sha(Path(snapshot["path"]) / "model.safetensors")
    (OUT / "comparison.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"status": result["status"], "validations": validations, "experimental_gates": {k: v["experimental_gates"] for k, v in result["devices"].items()}, "timing_query_kinds": result["timing_query_kinds"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
