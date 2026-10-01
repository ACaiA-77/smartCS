from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from rag.models import RetrievalHit
from scripts.diagnose_rag_stages import check_parity, classify, collect_candidates, probe_first_stage, validate_agent_inputs
from scripts.evaluate_rag_retrieval import load_queries


def trace(qrels, dense, fused, ce):
    return {"query_id": "q", "query": "fixed", "domain": "agent_engineering", "kind": "semantic",
            "qrels": qrels, "dense20": dense, "bm25_20": [], "rrf20": fused, "ce20": ce}


@pytest.mark.parametrize("row, expected", [
    (trace({"a": 2}, [], [], []), "FIRST_STAGE_MISS"),
    (trace({"a": 2}, ["a"], [f"x{i}" for i in range(20)], []), "FUSION_DROP"),
    (trace({"a": 2}, ["a"], ["a"], [f"x{i}" for i in range(10)] + ["a"]), "RERANK_DROP"),
    (trace({"a": 2}, ["a"], ["a"], ["x", "a"]), "ORDERING_ONLY"),
    (trace({"a": 2}, ["a"], ["a"], ["a"]), "PASS"),
])
def test_stage_boundaries(row, expected):
    assert classify(row)["bucket"] == expected


def test_mixed_qrels_preserve_individual_failures_and_earliest_bucket():
    row = trace({"a": 2, "b": 1, "c": 2, "d": 1}, ["b", "c", "d"], ["c", "d"],
                ["d"] + [f"x{i}" for i in range(9)] + ["c"])
    result = classify(row)
    assert result["bucket"] == "FIRST_STAGE_MISS"
    assert result["qrel_stages"] == {"a": "FIRST_STAGE_MISS", "b": "FUSION_DROP",
                                     "c": "RERANK_DROP", "d": "RETAINED"}


def test_graded_ordering_is_not_mistaken_for_pass():
    assert classify(trace({"a": 1, "b": 2}, ["a", "b"], ["a", "b"], ["a", "b"]))["bucket"] == "ORDERING_ONLY"


class StubRetriever:
    def __init__(self):
        self.calls = []

    def dense_search(self, query, *, domains, top_k):
        self.calls.append(("dense", domains, top_k))
        return [RetrievalHit(str(i), "agent_engineering", i + 1, 20 - i) for i in range(20)]

    def sparse_search(self, query, *, domains, top_k):
        self.calls.append(("bm25", domains, top_k))
        return []

    def rerank(self, query, candidates, top_k):
        self.calls.append(("rerank", None, top_k))
        result = list(reversed(candidates))
        for rank, hit in enumerate(result, 1):
            hit.rank = rank
            hit.rerank_score = hit.score = float(21 - rank)
        return result[:top_k]


def test_collects_global20_and_preserves_fusion_ranks_before_mutating_rerank():
    retriever = StubRetriever()
    row = collect_candidates(retriever, SimpleNamespace(query_id="q", query="fixed", domain="agent_engineering",
                                                        kind="semantic", qrels={"0": 2}))
    assert retriever.calls == [("dense", None, 20), ("bm25", None, 20), ("rerank", None, 20)]
    assert row["rrf20"] == [str(i) for i in range(20)]
    assert row["ce20"] == list(reversed(row["rrf20"]))
    first = row["candidates"][0]
    assert first["rrf_rank"] == 1 and first["rerank_rank"] == 20
    assert first["rrf_score"] == pytest.approx(1 / 61)
    assert classify(row)["bucket"] == "RERANK_DROP"


def test_probe_only_runs_for_first_stage_misses():
    retriever = StubRetriever()
    row = trace({"0": 2}, ["0"], ["0"], ["0"])
    assert probe_first_stage(retriever, row, classify(row)) is None
    assert retriever.calls == []
    row = trace({"0": 2}, [], [], [])
    probe = probe_first_stage(retriever, row, classify(row))
    assert retriever.calls == [(kind, domain, depth) for domain, depth in
                               (("agent_engineering", 20), (None, 40), ("agent_engineering", 40))
                               for kind in ("dense", "bm25")]
    assert probe["qrels"]["0"] == {"subreason": "GLOBAL_COMPETITION_DROP", "depth40": "RECOVERED_GLOBAL_40"}


def test_baseline_parity_fails_closed():
    row = trace({"a": 2}, ["a"], ["a"], ["a"])
    row["rankings"] = {"dense": ["a"], "bm25": [], "hybrid_rrf": ["a"], "hybrid_rerank": ["a"]}
    check_parity(row, deepcopy(row))
    baseline = deepcopy(row)
    baseline["rankings"]["hybrid_rerank"] = ["b"]
    with pytest.raises(ValueError, match="baseline parity mismatch"):
        check_parity(row, baseline)


def test_frozen_agent_inputs_are_30_queries_55_qrels_and_match_per_query():
    queries = [row for row in load_queries(Path("benchmarks/rag")) if row.domain == "agent_engineering"]
    baseline = json.loads(Path("tests/fixtures/rag/corrected_baseline/metrics_per_query.json").read_text(encoding="utf-8"))
    checked = validate_agent_inputs(queries, baseline)
    assert len(checked) == 30
    assert sum(len(row["qrels"]) for row in checked.values()) == 55
    changed = deepcopy(baseline)
    agent = next(row for row in changed if row["domain"] == "agent_engineering")
    key = next(iter(agent["qrels"]))
    agent["qrels"][key] = 1 if agent["qrels"][key] == 2 else 2
    with pytest.raises(ValueError, match="frozen input parity mismatch"):
        validate_agent_inputs(queries, changed)
    with pytest.raises(ValueError, match="30 Agent queries / 55 qrels"):
        validate_agent_inputs(queries[:-1], baseline)
