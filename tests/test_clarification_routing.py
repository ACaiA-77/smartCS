from agents.supervisor import route_after_intent


def test_route_after_intent_low_confidence_uses_dedicated_clarification_node():
    state = {"needs_clarification": True, "intent": "knowledge_rag"}

    assert route_after_intent(state) == "clarification"
