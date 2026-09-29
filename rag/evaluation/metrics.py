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


def index_relevance_groups(
    groups: Iterable[Mapping[str, Any]],
) -> tuple[dict[str, str], dict[str, int]]:
    """Validate one query's fact groups and index every member chunk."""

    member_to_group: dict[str, str] = {}
    grades: dict[str, int] = {}
    for group in groups:
        if not isinstance(group, Mapping):
            raise ValueError("relevance group must be an object")
        group_id = group.get("group_id")
        grade = group.get("relevance")
        members = group.get("member_chunk_ids")
        canonical = group.get("canonical_chunk_id")
        details = group.get("members")
        if not isinstance(group_id, str) or not group_id or group_id in grades:
            raise ValueError(f"invalid or duplicate group_id: {group_id!r}")
        if type(grade) is not int or grade not in (1, 2):
            raise ValueError(f"invalid relevance for {group_id}")
        if not isinstance(members, list) or not members or any(
            not isinstance(member, str) or not member for member in members
        ) or len(set(members)) != len(members):
            raise ValueError(f"invalid member_chunk_ids for {group_id}")
        if canonical not in members:
            raise ValueError(f"canonical_chunk_id is not a member of {group_id}")
        if not isinstance(details, list) or len(details) != len(members) or any(
            not isinstance(member, Mapping)
            or member.get("chunk_id") != chunk_id
            or type(member.get("relevance")) is not int
            or member["relevance"] != grade
            for member, chunk_id in zip(details, members)
        ):
            raise ValueError(f"mixed or inconsistent members for {group_id}")
        for member in members:
            if member in member_to_group:
                raise ValueError(f"chunk belongs to multiple groups: {member}")
            member_to_group[member] = group_id
        grades[group_id] = grade
    if not grades:
        raise ValueError("query has no relevance groups")
    return member_to_group, grades


def evaluate_grouped_ranking(
    retrieved: Iterable[str],
    relevance_groups: Iterable[Mapping[str, Any]],
    *,
    cutoffs: tuple[int, ...] = (1, 3, 5, 10),
) -> dict[str, float]:
    """Score distinct answer facts at their first actual retrieved rank."""

    member_to_group, grades = index_relevance_groups(relevance_groups)
    ranking = _unique(retrieved)
    ideal = sorted(grades.values(), reverse=True)
    results: dict[str, float] = {}
    for cutoff in cutoffs:
        credited: set[str] = set()
        first_rank = 0
        dcg = 0.0
        for rank, chunk_id in enumerate(ranking[:cutoff], start=1):
            group_id = member_to_group.get(chunk_id)
            if group_id is None or group_id in credited:
                continue
            credited.add(group_id)
            if not first_rank:
                first_rank = rank
            dcg += (2 ** grades[group_id] - 1) / math.log2(rank + 1)
        idcg = sum(
            (2**grade - 1) / math.log2(rank + 1)
            for rank, grade in enumerate(ideal[:cutoff], start=1)
        )
        results[f"recall@{cutoff}"] = len(credited) / len(grades)
        results[f"mrr@{cutoff}"] = 1.0 / first_rank if first_rank else 0.0
        results[f"ndcg@{cutoff}"] = dcg / idcg if idcg else 0.0
    return results


def aggregate_metrics(rows: Iterable[Mapping[str, Any]]) -> dict[str, float]:
    """Macro-average metric rows, returning zero for an empty set."""

    values = list(rows)
    if not values:
        return {}
    keys = sorted({str(key) for row in values for key in row})
    return {key: sum(float(row.get(key, 0.0)) for row in values) / len(values) for key in keys}


__all__ = ["aggregate_metrics", "evaluate_ranking", "evaluate_grouped_ranking", "index_relevance_groups"]
