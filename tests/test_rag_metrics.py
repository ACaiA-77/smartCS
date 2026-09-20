from __future__ import annotations

import pytest

from rag.evaluation.metrics import aggregate_metrics, evaluate_ranking


def test_graded_metrics_match_hand_calculation():
    values = evaluate_ranking(["noise", "a", "b", "a"], {"a": 2, "b": 1, "c": 0})
    assert values["recall@1"] == pytest.approx(0.0)
    assert values["recall@3"] == pytest.approx(1.0)
    assert values["mrr@3"] == pytest.approx(0.5)
    ideal = 3.0 + 1.0 / 1.5849625007
    dcg = 3.0 / 1.5849625007 + 1.0 / 2.0
    assert values["ndcg@3"] == pytest.approx(dcg / ideal)


def test_duplicate_results_do_not_inflate_recall_and_empty_qrels_are_zero():
    values = evaluate_ranking(["a", "a", "a"], {"a": 2, "b": 1})
    assert values["recall@10"] == pytest.approx(0.5)
    assert evaluate_ranking(["a"], {})["ndcg@10"] == 0.0


def test_aggregate_is_macro_average():
    assert aggregate_metrics([{"recall@1": 1.0}, {"recall@1": 0.0}]) == {"recall@1": 0.5}
