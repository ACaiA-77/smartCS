"""Offline metric calculation for versioned intent-routing golden cases."""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

_SECURITY_SECONDARIES = {"account_security", "fraud_report", "sensitive_data", "prohibited_request"}


def _safe_divide(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


def _macro_f1(expected: list[str], predicted: list[str]) -> float:
    labels = sorted(set(expected) | set(predicted))
    if not labels:
        return 0.0
    values: list[float] = []
    for label in labels:
        true_positive = sum(actual == label and guess == label for actual, guess in zip(expected, predicted))
        false_positive = sum(actual != label and guess == label for actual, guess in zip(expected, predicted))
        false_negative = sum(actual == label and guess != label for actual, guess in zip(expected, predicted))
        precision = _safe_divide(true_positive, true_positive + false_positive)
        recall = _safe_divide(true_positive, true_positive + false_negative)
        values.append(_safe_divide(2 * precision * recall, precision + recall))
    return sum(values) / len(values)


def evaluate_predictions(cases: list[dict[str, Any]], predictions: list[dict[str, Any]]) -> dict[str, float | int]:
    """Return deterministic quality metrics for aligned golden cases and predictions."""
    by_id = {str(prediction.get("id")): prediction for prediction in predictions}
    expected_labels: list[str] = []
    predicted_labels: list[str] = []
    correct = 0
    clarification_correct = 0
    entity_correct = 0
    security_total = 0
    security_correct = 0

    for case in cases:
        expected = case.get("expected", {})
        prediction = by_id.get(str(case.get("id")), {})
        expected_label = str(expected.get("secondary_intent", ""))
        predicted_label = str(prediction.get("secondary_intent", ""))
        expected_labels.append(expected_label)
        predicted_labels.append(predicted_label)
        if expected_label == predicted_label and expected.get("suggested_agent") == prediction.get("suggested_agent"):
            correct += 1
        if bool(expected.get("needs_clarification")) == bool(prediction.get("needs_clarification")):
            clarification_correct += 1
        if prediction and dict(expected.get("entities", {})) == dict(prediction.get("entities", {})):
            entity_correct += 1
        if expected_label in _SECURITY_SECONDARIES:
            security_total += 1
            if predicted_label == expected_label:
                security_correct += 1

    count = len(cases)
    return {
        "count": count,
        "accuracy": _safe_divide(correct, count),
        "macro_f1": _macro_f1(expected_labels, predicted_labels),
        "security_recall": _safe_divide(security_correct, security_total),
        "clarification_accuracy": _safe_divide(clarification_correct, count),
        "entity_exact_match": _safe_divide(entity_correct, count),
    }


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate SmartCS intent-routing predictions against golden cases.")
    parser.add_argument("--cases", default="evaluation/intent_routing_cases.jsonl")
    parser.add_argument("--predictions", required=True, help="JSONL predictions with id and normalized decision fields")
    args = parser.parse_args()
    print(json.dumps(evaluate_predictions(_load_jsonl(Path(args.cases)), _load_jsonl(Path(args.predictions))), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
