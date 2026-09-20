"""Small, explicit ranking metrics used by the Round 3 benchmark."""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from typing import Any


def _unique(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        value = str(item)
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


def evaluate_ranking(
    retrieved: Iterable[str], qrels: Mapping[str, int], *, cutoffs: tuple[int, ...] = (1, 3, 5, 10)
) -> dict[str, float]:
    """Evaluate one ranking against graded qrels.

    Repeated retrieved IDs count once, relevance is ``>= 1`` for Recall/MRR,
    and nDCG uses the graded gain ``2 ** relevance - 1``.
    """

    ranking = _unique(retrieved)
    relevant = {str(chunk_id) for chunk_id, value in qrels.items() if int(value) >= 1}
    results: dict[str, float] = {}
    for cutoff in cutoffs:
        top = ranking[:cutoff]
        results[f"recall@{cutoff}"] = (
            sum(item in relevant for item in top) / len(relevant) if relevant else 0.0
        )
        reciprocal = next(
            (1.0 / position for position, item in enumerate(top, start=1) if item in relevant),
            0.0,
        )
        results[f"mrr@{cutoff}"] = reciprocal
        gains = [max(0, int(qrels.get(item, 0))) for item in top]
        dcg = sum((2**gain - 1) / math.log2(position + 1) for position, gain in enumerate(gains, start=1))
        ideal = sorted((max(0, int(value)) for value in qrels.values()), reverse=True)[:cutoff]
        idcg = sum((2**gain - 1) / math.log2(position + 1) for position, gain in enumerate(ideal, start=1))
        results[f"ndcg@{cutoff}"] = dcg / idcg if idcg else 0.0
    return results


def aggregate_metrics(rows: Iterable[Mapping[str, Any]]) -> dict[str, float]:
    """Macro-average metric rows, returning zero for an empty set."""

    values = list(rows)
    if not values:
        return {}
    keys = sorted({str(key) for row in values for key in row})
    return {key: sum(float(row.get(key, 0.0)) for row in values) / len(values) for key in keys}


__all__ = ["aggregate_metrics", "evaluate_ranking"]
