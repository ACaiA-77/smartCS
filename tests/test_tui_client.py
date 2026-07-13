from __future__ import annotations

import json

import httpx
import pytest

from tui.client import AgentApiClient, AgentApiError


def test_chat_posts_expected_payload_and_returns_response_fields():
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["payload"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            json={
                "response": "你好，我是 SmartCS。",
                "session_id": "tui_12345678",
                "intent": "ticket_handler",
                "secondary_intent": "repair_request",
                "response_mode": "collect_ticket_details",
                "needs_clarification": False,
                "compliance_passed": True,
            },
        )

    client = AgentApiClient(
        base_url="http://localhost:8000/",
        timeout=5,
        transport=httpx.MockTransport(handler),
    )

    result = client.chat("你好", user_id="user_001", session_id="tui_12345678")

    assert captured["method"] == "POST"
    assert captured["path"] == "/api/chat"
    assert captured["payload"] == {
        "message": "你好",
        "user_id": "user_001",
        "session_id": "tui_12345678",
    }
    assert result.response == "你好，我是 SmartCS。"
    assert result.intent == "ticket_handler"
    assert result.secondary_intent == "repair_request"
    assert result.response_mode == "collect_ticket_details"
    assert result.needs_clarification is False
    assert result.compliance_passed is True
    assert result.session_id == "tui_12345678"


def test_health_and_history_use_existing_fastapi_endpoints():
    requested_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_paths.append(request.url.path)
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "healthy", "version": "1.0.0"})
        if request.url.path == "/api/history/tui_12345678":
            return httpx.Response(
                200,
                json={
                    "session_id": "tui_12345678",
                    "messages": [{"role": "user", "content": "你好"}],
                },
            )
        return httpx.Response(404, json={"detail": "not found"})

    client = AgentApiClient(
        base_url="http://localhost:8000",
        transport=httpx.MockTransport(handler),
    )

    assert client.health()["status"] == "healthy"
    assert client.history("tui_12345678")["messages"][0]["content"] == "你好"
    assert requested_paths == ["/health", "/api/history/tui_12345678"]


def test_http_errors_are_raised_with_readable_detail():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"detail": "处理失败: provider blocked"})

    client = AgentApiClient(
        base_url="http://localhost:8000",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(AgentApiError, match="处理失败: provider blocked"):
        client.chat("你好", user_id="user_001", session_id="tui_12345678")
