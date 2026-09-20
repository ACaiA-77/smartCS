"""Create leakage and failure reports without inventing metrics."""

from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from pathlib import Path


def _tokens(value: str) -> set[str]:
    return {item.lower() for item in re.findall(r"[a-z0-9-]+|[\u4e00-\u9fff]{2,4}", value)}


def write_reports(benchmark_root: Path, output_root: Path, index_root: Path = Path("artifacts/rag_round1/indexes")) -> None:
    output_root.mkdir(parents=True, exist_ok=True)
    queries = [json.loads(line) for line in (benchmark_root / "queries.jsonl").read_text(encoding="utf-8").splitlines()]
    qrels = [json.loads(line) for line in (benchmark_root / "qrels.jsonl").read_text(encoding="utf-8").splitlines()]
    by_query = defaultdict(list)
    chunks = {}
    for domain in ("apple_support", "agent_engineering"):
        path = index_root / domain / "chunks.jsonl"
        chunks.update({
            (domain, row["chunk_id"]): row
            for row in (json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip())
        })
    for row in qrels:
        by_query[row["query_id"]].append(row)
    overlaps = []
    for query in queries:
        qtokens = _tokens(query["query"])
        linked = by_query[query["query_id"]]
        linked_chunks = [chunks[(row["domain"], row["chunk_id"])] for row in linked]
        title_copy = any(
            " ".join(str(query["query"]).lower().split())
            == " ".join(str(chunk.get("title", "")).lower().split())
            for chunk in linked_chunks
        )
        source_overlaps = [
            len(qtokens & _tokens(str(chunk.get("content") or chunk.get("retrieval_text") or ""))) / len(qtokens)
            if qtokens
            else 0.0
            for chunk in linked_chunks
        ]
        overlaps.append(
            {
                "query_id": query["query_id"],
                "domain": query["domain"],
                "kind": query["kind"],
                "query_token_count": len(qtokens),
                "qrel_sources": sorted({row["source"] for row in linked}),
                "exact_title_copy": title_copy,
                "max_qrel_lexical_overlap": round(max(source_overlaps, default=0.0), 4),
                "high_lexical_overlap": max(source_overlaps, default=0.0) >= 0.9,
                "authoring": query.get("authoring", "unknown"),
            }
        )
    (output_root / "query_overlap_report.json").write_text(
        json.dumps(
            {
                "query_count": len(overlaps),
                "duplicate_query_count": len(queries) - len({row["query"] for row in queries}),
                "exact_title_copy_count": sum(row["exact_title_copy"] for row in overlaps),
                "high_lexical_overlap_count": sum(row["high_lexical_overlap"] for row in overlaps),
                "threshold": "query tokens overlapping qrel content >= 0.9",
                "rows": overlaps,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    validation_path = output_root / "model_validation.json"
    metrics_path = output_root / "metrics.json"
    per_query_path = output_root / "metrics_per_query.json"
    validation = json.loads(validation_path.read_text(encoding="utf-8")) if validation_path.exists() else {}
    metrics = json.loads(metrics_path.read_text(encoding="utf-8")) if metrics_path.exists() else {}
    per_query = json.loads(per_query_path.read_text(encoding="utf-8")) if per_query_path.exists() else []
    ready = validation.get("status") == "ready" and metrics and metrics.get("status") != "BLOCKED"
    if ready:
        def first_case(predicate):
            return next((row for row in per_query if predicate(row)), None)

        def describe(row, variant):
            values = row["variants"][variant]
            return (
                f"{row['query_id']} [{row['domain']}] {row['query']} — "
                f"{variant} recall@10={values['recall@10']:.3f}, "
                f"mrr@10={values['mrr@10']:.3f}"
            )

        dense_success_bm25_failure = first_case(
            lambda row: row["variants"]["dense"]["recall@10"] > 0
            and row["variants"]["bm25"]["recall@10"] == 0
        )
        bm25_success_dense_failure = first_case(
            lambda row: row["variants"]["bm25"]["recall@10"] > 0
            and row["variants"]["dense"]["recall@10"] == 0
        )
        rrf_improves = first_case(
            lambda row: row["variants"]["hybrid_rrf"]["recall@10"]
            > max(row["variants"]["dense"]["recall@10"], row["variants"]["bm25"]["recall@10"])
        )
        rerank_improves = first_case(
            lambda row: row["variants"]["hybrid_rerank"]["recall@10"]
            > row["variants"]["hybrid_rrf"]["recall@10"]
        )
        rerank_regresses = first_case(
            lambda row: row["variants"]["hybrid_rerank"]["recall@10"]
            < row["variants"]["hybrid_rrf"]["recall@10"]
        )
        cross_domain = first_case(
            lambda row: any(row["variants"][variant]["wrong_domain_rate@10"] > 0 for variant in row["variants"])
        )
        no_recall = first_case(
            lambda row: all(row["variants"][variant]["recall@10"] == 0 for variant in row["variants"])
        )
        case_lines = []
        for label, row, variant in (
            ("Dense succeeds while BM25 misses", dense_success_bm25_failure, "dense"),
            ("BM25 succeeds while Dense misses", bm25_success_dense_failure, "bm25"),
            ("RRF improves recall", rrf_improves, "hybrid_rrf"),
            ("Reranker improves recall", rerank_improves, "hybrid_rerank"),
            ("Reranker regresses recall", rerank_regresses, "hybrid_rerank"),
            ("Cross-domain contamination", cross_domain, "hybrid_rerank"),
            ("Top-K misses all qrels", no_recall, "dense"),
        ):
            if row:
                case_lines.append(f"- {label}: {describe(row, variant)}")
        text = (
            "# Round 3 retrieval failure analysis\n\n"
            "Real BGE-M3 and BGE Cross-Encoder models loaded offline and the production benchmark completed.\n\n"
            "- Dense-only, BM25-only, Hybrid RRF, and Hybrid + rerank metrics are in `metrics.json`.\n"
            "- Cross-domain contamination is reported as `wrong_domain_rate@1/3/5/10`.\n"
            "- Query authoring and qrel audit evidence is in `query_overlap_report.json`.\n"
            "\n## Observed benchmark cases\n\n"
            + ("\n".join(case_lines) if case_lines else "No matching case was observed in the saved per-query report.")
            + "\n"
        )
    else:
        text = (
            "# Round 3 retrieval failure analysis\n\n"
            "The required real BGE-M3 and BGE Cross-Encoder models were not executable in this environment, "
            "so final production metrics and variant comparisons are intentionally not generated.\n\n"
            "- Dense-only: BLOCKED pending real BGE-M3 index/query execution.\n"
            "- BM25-only: implementation is covered by Round1/2 tests; no final benchmark claim is made here.\n"
            "- Hybrid RRF: BLOCKED from final comparison until the production embedding is available.\n"
            "- Hybrid + rerank: BLOCKED pending real BGE Cross-Encoder execution.\n"
        )
    (output_root / "failure_analysis.md").write_text(text, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--benchmark-root", type=Path, default=Path("benchmarks/rag"))
    parser.add_argument("--output-root", type=Path, default=Path("artifacts/rag_round3"))
    parser.add_argument("--index-root", type=Path, default=Path("artifacts/rag_round1/indexes"))
    args = parser.parse_args()
    write_reports(args.benchmark_root, args.output_root, args.index_root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
