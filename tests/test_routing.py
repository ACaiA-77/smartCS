"""路由函数单元测试。"""

from __future__ import annotations

from agents.supervisor import route_after_intent, route_to_agent


def test_route_to_agent_knowledge_rag():
    assert route_to_agent({"intent": "knowledge_rag"}) == "knowledge_rag"


def test_route_to_agent_ticket_handler():
    assert route_to_agent({"intent": "ticket_handler"}) == "ticket_handler"


def test_route_to_agent_compliance_checker():
    assert route_to_agent({"intent": "compliance_checker"}) == "compliance_check"


def test_route_to_agent_default_fallback():
    assert route_to_agent({"intent": "unknown"}) == "knowledge_rag"
    assert route_to_agent({}) == "knowledge_rag"


def test_route_after_intent_low_confidence_goes_compliance():
    state = {"needs_clarification": True, "intent": "knowledge_rag"}
    assert route_after_intent(state) == "compliance_check"


def test_route_after_intent_normal_dispatches_by_intent():
    state = {"needs_clarification": False, "intent": "ticket_handler"}
    assert route_after_intent(state) == "ticket_handler"
