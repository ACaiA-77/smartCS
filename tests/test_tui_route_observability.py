from tui.app import format_chat_result
from tui.client import ChatResult


def test_tui_shows_semantic_route_and_response_mode():
    result = ChatResult(
        response="请补充地区。",
        session_id="tui_12345678",
        intent="ticket_handler",
        secondary_intent="repair_request",
        response_mode="collect_ticket_details",
        needs_clarification=False,
        compliance_passed=True,
    )

    formatted = format_chat_result(result)

    assert "target=ticket_handler" in formatted
    assert "secondary=repair_request" in formatted
    assert "response_mode=collect_ticket_details" in formatted
