"""显式 ChatOrchestrator 路由契约测试。"""

from __future__ import annotations

from agents.orchestrator import ChatOrchestrator


def test_route_to_agent_knowledge_rag():
    assert ChatOrchestrator._route_name("knowledge_rag") == "knowledge_rag"


def test_route_to_agent_ticket_handler():
    assert ChatOrchestrator._route_name("ticket_handler") == "ticket_handler"


def test_route_to_agent_order_query_uses_order_query_handler():
    assert ChatOrchestrator._route_name("order_query") == "ticket_handler"


def test_route_to_agent_compliance_checker():
    assert ChatOrchestrator._route_name("compliance_checker") == "compliance_check"


def test_route_to_agent_default_fallback():
    assert ChatOrchestrator._route_name("unknown") == "clarification"
    assert ChatOrchestrator._route_name("") == "clarification"


def test_route_to_conversation():
    assert ChatOrchestrator._route_name("conversation") == "conversation"


def test_explicit_orchestrator_maps_refund_variants_to_one_handler():
    for intent in ("refund_handler", "refund_request", "refund_confirm", "refund_cancel"):
        assert ChatOrchestrator._route_name(intent) == "refund_handler"
