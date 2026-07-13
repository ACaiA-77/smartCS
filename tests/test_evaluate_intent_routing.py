"""Tests for deterministic intent-routing evaluation metrics."""

from __future__ import annotations

from scripts.evaluate_intent_routing import evaluate_predictions


def test_evaluation_reports_required_metrics_and_handles_security_and_entities():
    cases = [
        {
            "id": "security-1",
            "expected": {
                "secondary_intent": "account_security",
                "suggested_agent": "compliance_checker",
                "needs_clarification": False,
                "entities": {"account_issue": "suspicious_login"},
            },
        },
        {
            "id": "policy-1",
            "expected": {
                "secondary_intent": "subscription_policy",
                "suggested_agent": "knowledge_rag",
                "needs_clarification": True,
                "entities": {"subscription": "AppleCare"},
            },
        },
    ]
    predictions = [
        {
            "id": "security-1",
            "secondary_intent": "account_security",
            "suggested_agent": "compliance_checker",
            "needs_clarification": False,
            "entities": {"account_issue": "suspicious_login"},
        },
        {
            "id": "policy-1",
            "secondary_intent": "subscription_policy",
            "suggested_agent": "knowledge_rag",
            "needs_clarification": True,
            "entities": {"subscription": "AppleCare"},
        },
    ]

    metrics = evaluate_predictions(cases, predictions)

    assert metrics == {
        "count": 2,
        "accuracy": 1.0,
        "macro_f1": 1.0,
        "security_recall": 1.0,
        "clarification_accuracy": 1.0,
        "entity_exact_match": 1.0,
    }


def test_evaluation_marks_missing_predictions_as_incorrect():
    cases = [
        {
            "id": "refund-1",
            "expected": {
                "secondary_intent": "refund_request",
                "suggested_agent": "ticket_handler",
                "needs_clarification": False,
                "entities": {},
            },
        }
    ]

    metrics = evaluate_predictions(cases, [])

    assert metrics["count"] == 1
    assert metrics["accuracy"] == 0.0
    assert metrics["entity_exact_match"] == 0.0
