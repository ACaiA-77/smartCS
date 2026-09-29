from copy import deepcopy

import pytest

from rag.evaluation.metrics import evaluate_ranking
from scripts import evaluate_rerank_safeguard as safeguard


def candidates():
    return [{"chunk_id": str(i), "domain": "agent_engineering", "rrf_rank": i + 1, "rerank_rank": 20 - i}
            for i in range(20)]


def test_fixed_equal_rank_fusion_ties_and_top10():
    rows = candidates()
    # Symmetric reciprocal scores tie; CE rank resolves each pair.
    expected = ["19", "0", "18", "1", "17", "2", "16", "3", "15", "4"]
    assert safeguard.rank_fusion(rows) == expected
    assert safeguard.rank_fusion(list(reversed(rows))) == expected
    assert len(safeguard.rank_fusion(rows)) == 10


def test_rejects_incomplete_rank_evidence():
    rows = candidates()
    rows[0]["rerank_rank"] = 19
    with pytest.raises(ValueError, match="incomplete"):
        safeguard.rank_fusion(rows)


def test_metrics_use_graded_qrels_and_real_candidate_domains():
    rows = candidates()
    rows[1]["domain"] = "apple_support"
    values = safeguard.metrics(["1", "0"], {"0": 2}, rows, "agent_engineering")
    assert values["recall@10"] == 1
    assert values["mrr@10"] == .5
    assert values["ndcg@10"] == pytest.approx(1 / 1.584962500721156)
    assert values["wrong_domain_rate@10"] == .5


@pytest.mark.parametrize("new_drops, after_drops, quality_drop", [(1, 3, False), (0, 4, False), (0, 3, True)])
def test_gate_rejects_regressions_or_no_drop_recovery(new_drops, after_drops, quality_drop):
    before = {key: .8 for key in safeguard.QUALITY} | {"wrong_domain_rate@10": 0}
    after = dict(before, **{"recall@10": .9, "mrr@10": .7 if quality_drop else .8})
    assert safeguard.agent_gate(before, after, before_drops=4, after_drops=after_drops,
                                new_drops=new_drops)["status"] == "RERANK_SAFEGUARD_NOT_SUPPORTED"


def test_replay_accounts_for_recovered_and_newly_lost_qrels_without_models(monkeypatch):
    monkeypatch.setitem(__import__("sys").modules, "sentence_transformers", None)
    rows = []
    ids = [str(i) for i in range(20)]
    for i in range(30):
        qrels = {"0": 2, "10": 1} if i < 25 else {"0": 2}
        metrics = evaluate_ranking(ids[::-1][:10], qrels) | {"wrong_domain_rate@10": 0}
        rows.append({"query_id": f"agent_{i+1:03d}", "query": "fixed", "domain": "agent_engineering",
            "kind": "semantic", "qrels": qrels, "candidates": candidates(), "rrf20": ids,
            "ce20": ids[::-1], "rankings": {"hybrid_rerank": ids[::-1][:10]},
            "variants": {"hybrid_rerank": metrics}})
    result = safeguard.evaluate(rows, deepcopy(rows))
    assert result["deltas"][0]["recovered_qrels"] == ["0"]
    assert result["deltas"][0]["new_qrel_drops"] == ["10"]
    assert result["gate"]["conditions"]["no_new_qrel_drop"] is False
    assert result["gate"]["status"] == "RERANK_SAFEGUARD_NOT_SUPPORTED"


def test_failed_agent_gate_returns_stop_code_for_downstream_apple_phase(monkeypatch):
    monkeypatch.setattr("sys.argv", ["evaluate_rerank_safeguard"])
    monkeypatch.setattr(safeguard, "run", lambda *args: {"gate": {"status": "RERANK_SAFEGUARD_NOT_SUPPORTED"}})
    assert safeguard.main() == 2
