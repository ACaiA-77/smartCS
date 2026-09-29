"""Select Dev transition cases from already saved A/B metrics, without models."""

import json
from pathlib import Path

a = {row["query_id"]: row for row in json.loads(Path("artifacts/rag_corrected_baseline_20260921/metrics_per_query.json").read_text(encoding="utf-8"))}
b = {row["query_id"]: row for row in json.loads(Path("artifacts/rag_global_sparse_candidate_dev_20260923/metrics_per_query.json").read_text(encoding="utf-8"))}
assert len(a) == len(b) == 60 and set(a) == set(b)
keys = ("recall@10", "mrr@10", "ndcg@10")
focus = []
ce_recall_loss = []
ce_any_loss = []
for query_id, old in a.items():
    new = b[query_id]
    assert (old["query"], old["domain"], old["qrels"]) == (new["query"], new["domain"], new["qrels"])
    assert old["rankings"]["dense"] == new["rankings"]["dense"]
    rrf_gain = any(new["variants"]["hybrid_rrf"][key] > old["variants"]["hybrid_rrf"][key] + 1e-12 for key in keys)
    ce_loss = any(new["variants"]["hybrid_rerank"][key] < old["variants"]["hybrid_rerank"][key] - 1e-12 for key in keys)
    if rrf_gain and ce_loss:
        focus.append(query_id)
    if ce_loss:
        ce_any_loss.append(query_id)
    if new["variants"]["hybrid_rerank"]["recall@10"] < old["variants"]["hybrid_rerank"]["recall@10"] - 1e-12:
        ce_recall_loss.append(query_id)
print(json.dumps({"query_count": 60, "rrf_gain_ce_loss_focus": focus,
                  "ce_any_loss": ce_any_loss, "ce_recall_loss": ce_recall_loss}, ensure_ascii=False))
