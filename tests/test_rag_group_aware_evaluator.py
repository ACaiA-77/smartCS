from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from rag.evaluation.evaluator import evaluate_variants
from rag.evaluation.metrics import evaluate_grouped_ranking, evaluate_ranking
from rag.evaluation.models import BenchmarkQuery
from rag.models import RetrievalHit
from scripts.evaluate_rag_retrieval import load_relevance_groups


def group(name, grade, *chunks, original_grade=None, reason=None):
    members = []
    for chunk in chunks:
        member = {"chunk_id": chunk, "relevance": grade}
        if original_grade is not None and chunk == chunks[-1]:
            member.update(original_relevance=original_grade, adjudication_reason=reason)
        members.append(member)
    return {
        "query_id": "q1", "group_id": name, "relevance": grade,
        "canonical_chunk_id": chunks[0], "member_chunk_ids": list(chunks),
        "members": members,
    }


def test_alias_matches_canonical_but_occupies_its_real_rank():
    groups = [group("A", 2, "a1", "a2"), group("B", 1, "b")]
    alias = evaluate_grouped_ranking(["noise", "a2"], groups)
    canonical = evaluate_grouped_ranking(["noise", "a1"], groups)
    assert alias == canonical
    assert alias["mrr@3"] == 0.5
    values = evaluate_grouped_ranking(["a1", "a2", "b"], groups)
    assert values["recall@1"] == 0.5
    assert values["recall@3"] == 1.0
    assert evaluate_grouped_ranking(["a1", "a2", "b"], groups, cutoffs=(2,))["recall@2"] == 0.5
    idcg = 3 + 1 / math.log2(3)
    dcg = 3 + 1 / math.log2(4)
    assert values["ndcg@3"] == pytest.approx(dcg / idcg)
    assert evaluate_grouped_ranking(["a2", "a1"], groups)["ndcg@3"] == pytest.approx(3 / idcg)


def test_independent_grade_one_facts_and_exact_chunk_deduplication():
    # Apple029's refund permission and cancellation permission are separate facts.
    groups = [group("refund", 1, "refund_chunk"), group("cancel", 1, "cancel_chunk")]
    assert evaluate_grouped_ranking(["refund_chunk"], groups)["recall@10"] == 0.5
    assert evaluate_grouped_ranking(["refund_chunk", "refund_chunk", "cancel_chunk"], groups)["recall@10"] == 1.0


def test_singleton_groups_match_existing_chunk_scoring():
    qrels = {"a": 2, "b": 1}
    groups = [group(chunk, grade, chunk) for chunk, grade in qrels.items()]
    ranking = ["noise", "b", "b", "a"]
    assert evaluate_grouped_ranking(ranking, groups) == evaluate_ranking(ranking, qrels)


@pytest.mark.parametrize("change", [
    lambda groups: groups.append(groups[0]),
    lambda groups: groups[0].update(member_chunk_ids=[]),
    lambda groups: groups[0].update(canonical_chunk_id="missing"),
    lambda groups: groups[0].update(relevance=3),
    lambda groups: groups.append(group("B", 1, "a")),
    lambda groups: groups[0]["members"][0].update(relevance=1),
])
def test_invalid_groups_fail_closed(change):
    groups = [group("A", 2, "a")]
    change(groups)
    with pytest.raises(ValueError):
        evaluate_grouped_ranking(["a"], groups)


def test_loader_checks_coverage_and_adjudication(tmp_path):
    path = tmp_path / "groups.jsonl"
    query = BenchmarkQuery("q1", "question", "agent_engineering", "semantic", {"a": 2, "b": 1})
    groups = [group("A", 2, "a", "b", original_grade=1, reason="diagram completes answer")]
    path.write_text("\n".join(json.dumps(row) for row in groups), encoding="utf-8")
    assert load_relevance_groups(path, queries=[query]) == {"q1": groups}
    del groups[0]["members"][1]["adjudication_reason"]
    path.write_text("\n".join(json.dumps(row) for row in groups), encoding="utf-8")
    with pytest.raises(ValueError, match="unexplained relevance change"):
        load_relevance_groups(path, queries=[query])
    with pytest.raises(ValueError, match="do not match qrels"):
        load_relevance_groups(path, queries=[BenchmarkQuery("q1", "question", "agent_engineering", "semantic", {"a": 2})])


def test_local_holdout_candidate_groups_are_loadable():
    root = Path("artifacts/rag_holdout_candidates_20260921")
    if not (root / "qrel_equivalence_groups.jsonl").is_file():
        pytest.skip("local holdout candidate artifacts are not checked in")
    query_rows = [json.loads(line) for line in (root / "proposed_queries.jsonl").read_text(encoding="utf-8").splitlines()]
    qrels: dict[str, dict[str, int]] = {}
    for line in (root / "proposed_qrels.jsonl").read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        qrels.setdefault(row["query_id"], {})[row["chunk_id"]] = row["relevance"]
    queries = [BenchmarkQuery(row["query_id"], row["query"], row["domain"], row["kind"], qrels[row["query_id"]]) for row in query_rows]
    groups = load_relevance_groups(root / "qrel_equivalence_groups.jsonl", queries=queries)
    assert len(groups) == 36
    assert sum(map(len, groups.values())) == 44


class _AliasRetriever:
    def dense_search(self, query, *, domains, top_k):
        return [RetrievalHit("alias", "apple_support", 1, 1.0)]

    def sparse_search(self, query, *, domains, top_k):
        return []

    def rerank(self, query, candidates, top_k):
        return candidates[:top_k]


def test_evaluate_variants_uses_explicit_group_mode_only():
    query = BenchmarkQuery("q1", "question", "apple_support", "semantic", {"canonical": 2, "alias": 2})
    retriever = _AliasRetriever()
    legacy = evaluate_variants(retriever, [query])
    grouped = evaluate_variants(
        retriever, [query], relevance_groups_by_query={"q1": [group("A", 2, "canonical", "alias")]}
    )
    assert legacy["per_query"][0]["variants"]["dense"]["recall@10"] == 0.5
    assert grouped["per_query"][0]["variants"]["dense"]["recall@10"] == 1.0
    assert "scoring_mode" not in legacy["per_query"][0]
    assert grouped["per_query"][0]["scoring_mode"] == "fact_group_v1"
