"""Check candidate integrity and query overlap without reading evaluation metrics."""

import json
import re
from collections import Counter
from difflib import SequenceMatcher
from pathlib import Path

from rag.evaluation.models import BenchmarkQuery
from scripts.evaluate_rag_retrieval import load_relevance_groups


ROOT = Path("artifacts/rag_cross_domain_sparse_challenge_candidates_20260923")
# Explicit judgments for every shared prior qrel; a new overlap fails the audit.
DISTINCT = {
    "challenge_agent_002": {"agent_011", "agent_015", "agent_019"},
    "challenge_agent_004": {"agent_001", "agent_002"},
    "challenge_agent_005": {"agent_021", "agent_022"},
    "challenge_agent_006": {"agent_003", "agent_015", "agent_019"},
    "challenge_agent_007": {"agent_003", "agent_007"},
    "challenge_agent_008": {"agent_027", "agent_028", "agent_029", "agent_030"},
    "challenge_agent_009": {"agent_001", "agent_002"},
    "challenge_agent_010": {"agent_001", "agent_002"},
    "challenge_agent_011": {"agent_026"},
    "challenge_apple_001": {"apple_022"},
    "challenge_apple_002": {"holdout_apple_039", "holdout_apple_040"},
    "challenge_apple_004": {"apple_026"},
    "challenge_apple_005": {"holdout_apple_025"},
    "challenge_apple_010": {"apple_024", "apple_025", "holdout_apple_023", "holdout_apple_036"},
    "challenge_apple_011": {"apple_018", "apple_019", "holdout_apple_033"},
    "challenge_apple_012": {"apple_029"},
}
PARTIAL = {
    ("challenge_agent_002", "agent_020"): "both cover Hook checks; challenge isolates full-suite cost at high trigger frequency",
    ("challenge_agent_003", "agent_025"): "same maintenance section; challenge isolates human semantic signoff, not Git/PR triggers",
    ("challenge_agent_004", "agent_020"): "both choose Hook scope; challenge asks why ordinary rules and deterministic checks stay outside Hooks",
    ("challenge_agent_006", "agent_004"): "PreToolUse timing is shared; PermissionRequest distinction is new",
    ("challenge_agent_007", "agent_004"): "event timing overlaps; challenge asks irreversibility of side effects",
    ("challenge_agent_007", "agent_008"): "both mention blocking; challenge compares pre/post side effects rather than all deny outcomes",
    ("challenge_agent_012", "agent_028"): "human authorization is one boundary of broader design flow",
    ("challenge_agent_012", "agent_029"): "human authorization is one boundary of broader executable-plan criteria",
    ("challenge_apple_003", "apple_015"): "Mac recovery topic overlaps; challenge asks the specific data-retention fact",
    ("challenge_apple_009", "apple_023"): "refund topic overlaps; challenge asks installment schedule recalculation",
}


def rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def normalize(value: str) -> str:
    return re.sub(r"[\W_]+", "", value.casefold())


def main() -> None:
    queries = rows(ROOT / "proposed_queries.jsonl")
    qrels = rows(ROOT / "proposed_qrels.jsonl")
    assert len(queries) == 24
    assert Counter((q["domain"], q["kind"]) for q in queries) == {
        (domain, kind): 4
        for domain in ("apple_support", "agent_engineering")
        for kind in ("semantic", "lexical", "confusing")
    }
    assert len({q["query_id"] for q in queries}) == len(queries)
    assert len({(r["query_id"], r["chunk_id"]) for r in qrels}) == len(qrels)
    by_query = {q["query_id"]: {} for q in queries}
    chunks = {
        row["chunk_id"]: row
        for domain in ("apple_support", "agent_engineering")
        for row in rows(Path("artifacts/rag_round3/production_indexes") / domain / "chunks.jsonl")
    }
    for rel in qrels:
        chunk = chunks[rel["chunk_id"]]
        assert rel["query_id"] in by_query and rel["relevance"] in (1, 2)
        assert all(rel[key] == chunk[key] for key in ("domain", "source", "heading_path"))
        by_query[rel["query_id"]][rel["chunk_id"]] = rel["relevance"]
    for q in queries:
        assert q["domain"] == next(r["domain"] for r in qrels if r["query_id"] == q["query_id"])
        assert 2 in by_query[q["query_id"]].values()
    objects = [BenchmarkQuery(q["query_id"], q["query"], q["domain"], q["kind"], by_query[q["query_id"]]) for q in queries]
    groups = load_relevance_groups(ROOT / "qrel_groups.jsonl", queries=objects)
    assert sum(map(len, groups.values())) == 32
    tag_counts = Counter(tag for q in queries for tag in q["mechanism_tags"])
    assert tag_counts["cross_domain_decoy"] >= 6
    assert tag_counts["supporting_evidence"] >= 6
    assert tag_counts["same_source_competition"] >= 6
    assert sum(len(items) > 1 for items in groups.values()) >= 4
    assert sum("supporting_evidence" in q["mechanism_tags"] and any(group["relevance"] == 1 for group in groups[q["query_id"]]) for q in queries) >= 4
    assert all("multi_fact" not in q["mechanism_tags"] or len(groups[q["query_id"]]) > 1 for q in queries)
    prior = rows(Path("benchmarks/rag_holdout_v1/queries.jsonl")) + rows(Path("benchmarks/rag/queries.jsonl"))
    similarities = sorted(
        ((SequenceMatcher(None, normalize(q["query"]), normalize(p["query"])).ratio(), q["query_id"], p["query_id"], p["query"])
         for q in queries for p in prior), reverse=True
    )
    assert all(normalize(q["query"]) != normalize(p["query"]) for q in queries for p in prior)
    report = ["# Candidate integrity and overlap audit", "", f"24 unique queries; 12/domain; 4 of each kind/domain; {len(qrels)} source-bound qrels; {sum(map(len, groups.values()))} valid fact groups.", "", "No exact normalized duplicate against Dev or Holdout query text. The similarity list below is for manual leakage review; no Holdout metrics or retrieval results were read.", "", "| Similarity | Candidate | Prior | Prior query |", "|---:|---|---|---|"]
    report.extend(f"| {score:.3f} | {candidate} | {prior_id} | {question} |" for score, candidate, prior_id, question in similarities[:12])
    (ROOT / "overlap_audit.md").write_text("\n".join(report) + "\n", encoding="utf-8")
    prior_by_id = {row["query_id"]: row for row in prior}
    prior_qrels = rows(Path("benchmarks/rag/qrels.jsonl")) + rows(Path("benchmarks/rag_holdout_v1/qrels.jsonl"))
    prior_chunks = {}
    for rel in prior_qrels:
        prior_chunks.setdefault(rel["query_id"], set()).add(rel["chunk_id"])
    shared = sorted(
        (q["query_id"], prior_id, sorted(set(by_query[q["query_id"]]) & chunk_ids))
        for q in queries for prior_id, chunk_ids in prior_chunks.items()
        if set(by_query[q["query_id"]]) & chunk_ids
    )
    distinct_pairs = {(candidate, prior_id) for candidate, prior_ids in DISTINCT.items() for prior_id in prior_ids}
    reviewed_pairs = distinct_pairs | set(PARTIAL)
    assert len(shared) == len(reviewed_pairs) == 44
    assert {(candidate, prior_id) for candidate, prior_id, _ in shared} == reviewed_pairs
    fact_report = ["# Fact-level prior overlap audit", "", "Source-fact review of every shared Dev/Holdout qrel chunk. Shared chunk does not imply shared answer. Zero SAME_INTENT; PARTIAL_OVERLAP rows retain an explicit independent test target. Holdout metrics were not opened.", "", "| Candidate | Prior | Shared qrels | Relation and independent target |", "|---|---|---|---|"]
    for candidate, prior_id, chunk_ids in shared:
        if (candidate, prior_id) in PARTIAL:
            relation = f"PARTIAL_OVERLAP — {PARTIAL[candidate, prior_id]}"
        else:
            relation = "DISTINCT_FACT — prior asks another fact from the same source section"
        fact_report.append(f"| {candidate} | {prior_id} ({prior_by_id[prior_id]['query']}) | {', '.join(chunk_ids)} | {relation} |")
    (ROOT / "fact_overlap_audit.md").write_text("\n".join(fact_report) + "\n", encoding="utf-8")
    print(f"PASS: 24 queries, {len(qrels)} qrels, {sum(map(len, groups.values()))} fact groups; max normalized overlap={similarities[0][0]:.3f}")
    print(f"FACT_REVIEW_ROWS={len(shared)}")


if __name__ == "__main__":
    main()
