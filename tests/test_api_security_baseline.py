from __future__ import annotations

import pytest
from pydantic import ValidationError

from api.health import build_readiness_report
from api.schemas import ChatRequest, ChatResponse, ToolCallRequest
from api.settings import AppSettings


def test_settings_use_explicit_local_cors_origins_by_default(monkeypatch):
    monkeypatch.delenv("CORS_ALLOWED_ORIGINS", raising=False)

    settings = AppSettings.from_env()

    assert settings.cors_allowed_origins == (
        "http://localhost:3000",
        "http://localhost:8000",
    )
    assert "*" not in settings.cors_allowed_origins


def test_settings_reject_wildcard_cors_origin(monkeypatch):
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "*")

    with pytest.raises(ValueError, match="wildcard"):
        AppSettings.from_env()


@pytest.mark.parametrize(
    "payload",
    [
        {"message": ""},
        {"message": "   "},
        {"message": "x" * 4001},
        {"message": "hello", "user_id": "../other-user"},
        {"message": "hello", "session_id": "bad/session"},
    ],
)
def test_chat_request_rejects_invalid_or_oversized_input(payload):
    with pytest.raises(ValidationError):
        ChatRequest.model_validate(payload)


def test_chat_request_strips_valid_message():
    request = ChatRequest.model_validate(
        {"message": "  hello  ", "user_id": "user_001", "session_id": "session-1"}
    )

    assert request.message == "hello"


def test_tool_call_request_requires_a_safe_name_and_object_arguments():
    with pytest.raises(ValidationError):
        ToolCallRequest.model_validate({"name": "", "arguments": {}})
    with pytest.raises(ValidationError):
        ToolCallRequest.model_validate({"name": "ticket/create", "arguments": {}})
    with pytest.raises(ValidationError):
        ToolCallRequest.model_validate({"name": "ticket_create", "arguments": []})


def test_readiness_report_fails_when_required_dependency_is_unavailable():
    report = build_readiness_report(
        graph_initialized=True,
        short_term={"ready": False, "mode": "memory"},
        long_term={"ready": True, "document_count": 3},
        require_redis=True,
        require_rag_index=True,
    )

    assert report["status"] == "not_ready"
    assert report["ready"] is False
    assert report["checks"]["short_term"]["required"] is True


def test_readiness_report_allows_explicit_development_fallbacks():
    report = build_readiness_report(
        graph_initialized=True,
        short_term={"ready": False, "mode": "memory"},
        long_term={"ready": True, "document_count": 0},
        require_redis=False,
        require_rag_index=False,
    )

    assert report["status"] == "ready"
    assert report["ready"] is True


def test_chat_response_exposes_semantic_route_metadata():
    response = ChatResponse(
        response="请补充地区。",
        session_id="s-1",
        intent="ticket_handler",
        secondary_intent="repair_request",
        response_mode="collect_ticket_details",
        needs_clarification=False,
        compliance_passed=True,
    )

    assert response.intent == "ticket_handler"
    assert response.secondary_intent == "repair_request"
    assert response.response_mode == "collect_ticket_details"
