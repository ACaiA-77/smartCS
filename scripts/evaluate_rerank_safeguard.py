"""Screen one fixed rank-fusion candidate using saved evidence, with no inference."""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import math
from pathlib import Path

from rag.evaluation.metrics import aggregate_metrics, evaluate_ranking
from rag.evaluation.report import write_json

QUALITY = ("recall@10", "mrr@10", "ndcg@10")
METRICS = (*QUALITY, "wrong_domain_rate@10")
KNOWN_DROPS = {"agent_002", "agent_006", "agent_021", "agent_023"}


def rank_fusion(candidates):
    """Equal k=60 reciprocal ranks; ties prefer CE, then RRF, then chunk ID."""
    selected = [row for row in candidates if row["rrf_rank"] <= 20]
    if not selected or len({row["chunk_id"] for row in selected}) != len(selected):
        raise ValueError("empty or duplicate candidate IDs")
    for field in ("rrf_rank", "rerank_rank"):
        if sorted(row[field] for row in selected) != list(range(1, len(selected) + 1)):
            raise ValueError(f"incomplete candidate {field}")
    return [row["chunk_id"] for row in sorted(selected, key=lambda row: (
        -(1 / (60 + row["rrf_rank"]) + 1 / (60 + row["rerank_rank"])),
        row["rerank_rank"], row["rrf_rank"], row["chunk_id"],
    ))[:10]]


def metrics(ids, qrels, candidates, domain):
    values = evaluate_ranking(ids, qrels, cutoffs=(10,))
    domains = {row["chunk_id"]: row["domain"] for row in candidates}
    values["wrong_domain_rate@10"] = sum(domains[key] != domain for key in ids) / len(ids) if ids else 0.0
    return values


def agent_gate(baseline, candidate, *, before_drops, after_drops, new_drops):
    conditions = {
        "quality_non_decreasing": all(candidate[key] >= baseline[key] - 1e-12 for key in QUALITY),
        "strict_quality_improvement": any(candidate[key] > baseline[key] + 1e-12 for key in QUALITY),
        "fewer_rerank_drops": after_drops < before_drops,
        "no_new_qrel_drop": new_drops == 0,
        "zero_wrong_domain": candidate["wrong_domain_rate@10"] == 0,
    }
    return {"status": "PASS" if all(conditions.values()) else "RERANK_SAFEGUARD_NOT_SUPPORTED", "conditions": conditions}


def evaluate(rows, baseline_rows):
    baseline_by_id = {row["query_id"]: row for row in baseline_rows if row["domain"] == "agent_engineering"}
    if len(rows) != 30 or len({row["query_id"] for row in rows}) != 30 or set(baseline_by_id) != {row["query_id"] for row in rows}:
        raise ValueError("expected exact 30-query baseline coverage")
    if sum(len(row["qrels"]) for row in rows) != 55:
        raise ValueError("expected 55 Agent qrels")
    deltas, counterfactual = [], []
    for row in rows:
        frozen = baseline_by_id[row["query_id"]]
        if any(row[key] != frozen[key] for key in ("query", "domain", "kind", "qrels", "rankings")):
            raise ValueError("frozen baseline input mismatch")
        old = row["ce20"][:10]
        if old != frozen["rankings"]["hybrid_rerank"] or set(row["ce20"]) != set(row["rrf20"]):
            raise ValueError("CE trace parity mismatch")
        new = rank_fusion(row["candidates"])
        if set(new) - set(row["rrf20"]):
            raise ValueError("candidate pool changed")
        before = metrics(old, row["qrels"], row["candidates"], row["domain"])
        if any(not math.isclose(before[key], frozen["variants"]["hybrid_rerank"][key], rel_tol=0, abs_tol=1e-12) for key in METRICS):
            raise ValueError("frozen metric parity mismatch")
        after = metrics(new, row["qrels"], row["candidates"], row["domain"])
        relevant = {key for key, grade in row["qrels"].items() if grade >= 1}
        recoveries = sorted((set(new) - set(old)) & relevant)
        losses = sorted((set(old) - set(new)) & relevant)
        improvements = [key for key in QUALITY if after[key] > before[key] + 1e-12]
        regressions = [key for key in QUALITY if after[key] < before[key] - 1e-12]
        delta = {"query_id": row["query_id"], "baseline": before, "candidate": after,
                 "delta": {key: after[key] - before[key] for key in METRICS},
                 "status": "regressed" if regressions else "improved" if improvements else "unchanged",
                 "improved_metrics": improvements, "regressed_metrics": regressions,
                 "recovered_qrels": recoveries, "new_qrel_drops": losses,
                 "rerank_drops_before": sorted((set(row["rrf20"]) - set(old)) & relevant),
                 "rerank_drops_after": sorted((set(row["rrf20"]) - set(new)) & relevant)}
        deltas.append(delta)
        counterfactual.append({"query_id": row["query_id"], "qrels": row["qrels"],
                               "baseline_top10": old, "candidate_top10": new})
    before = aggregate_metrics(row["baseline"] for row in deltas)
    after = aggregate_metrics(row["candidate"] for row in deltas)
    gate = agent_gate(before, after,
                      before_drops=sum(len(row["rerank_drops_before"]) for row in deltas),
                      after_drops=sum(len(row["rerank_drops_after"]) for row in deltas),
                      new_drops=sum(len(row["new_qrel_drops"]) for row in deltas))
    return {"baseline": before, "candidate": after, "gate": gate,
            "query_outcomes": dict(Counter(row["status"] for row in deltas)),
            "deltas": deltas, "counterfactual": counterfactual}


def run(evidence_root, baseline_root, output_root):
    protected_roots = [Path("benchmarks/rag"), baseline_root, evidence_root, Path("artifacts/rag_round3")]
    for path in protected_roots:
        out, source = output_root.resolve(), path.resolve()
        if out == source or out in source.parents or source in out.parents:
            raise ValueError("output overlaps frozen input")
    if (output_root / "agent_metrics.json").exists():
        raise ValueError("refusing to overwrite prior experiment")
    protected = [path for root in protected_roots for path in root.rglob("*") if path.is_file()]
    protected += [Path(name) for name in ("scripts/build_rag_benchmark.py", "tests/test_rag_benchmark.py",
                  "scripts/report_rag_round3.py", "tests/test_rag_report.py", "scripts/diagnose_rag_stages.py",
                  "tests/test_rag_stage_diagnosis.py")]
    before = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in protected}
    write_json(output_root / "frozen_hashes_before.json", before)
    rows = [json.loads(line) for line in (evidence_root / "candidate_evidence.jsonl").read_text(encoding="utf-8").splitlines()]
    baseline = json.loads((baseline_root / "metrics_per_query.json").read_text(encoding="utf-8"))
    result = evaluate(rows, baseline)
    write_json(output_root / "agent_counterfactual.json", result["counterfactual"])
    write_json(output_root / "per_query_delta.json", result["deltas"])
    write_json(output_root / "rerank_drop_recovery.json", [row for row in result["deltas"] if row["query_id"] in KNOWN_DROPS])
    report = {key: value for key, value in result.items() if key not in ("deltas", "counterfactual")}
    write_json(output_root / "agent_metrics.json", report)
    after = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in protected}
    write_json(output_root / "frozen_hashes_after.json", after)
    if before != after:
        raise ValueError("frozen files changed")
    text = "# Agent-only rerank safeguard screening\n\n"
    text += "One fixed formula: 1/(60+RRF rank) + 1/(60+CE rank). Ties prefer CE rank, RRF rank, chunk ID.\n\n"
    text += "No model calls. Same RRF20 candidate pool. No production changes or full-domain improvement claim.\n\n"
    text += "```json\n" + json.dumps(report, indent=2) + "\n```\n"
    text += "\nApple validation is prohibited unless every Agent gate condition passes.\n"
    (output_root / "summary.md").write_text(text, encoding="utf-8")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-root", type=Path, default=Path("artifacts/rag_stage_diagnosis_20260921"))
    parser.add_argument("--baseline-root", type=Path, default=Path("artifacts/rag_corrected_baseline_20260921"))
    parser.add_argument("--output-root", type=Path, default=Path("artifacts/rag_rerank_safeguard_20260921"))
    args = parser.parse_args()
    result = run(args.evidence_root, args.baseline_root, args.output_root)
    print(json.dumps(result, indent=2))
    # A failed scientific gate is preserved as a negative result and stops downstream execution.
    return 0 if result["gate"]["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
