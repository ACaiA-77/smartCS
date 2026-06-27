"""Supervisor 编排 Graph 集成测试（Mock LLM，无外部依赖）。"""

from __future__ import annotations

import pytest
from langchain_core.messages import HumanMessage

from agents.supervisor import SupervisorNode, create_supervisor_graph
from memory.long_term import LongTermMemory
from tests.conftest import MockLLM


@pytest.mark.asyncio
async def test_route_decision_does_not_call_llm(mock_llm, working_memory, base_state):
    supervisor = SupervisorNode(mock_llm, working_memory)
    out = await supervisor.route_decision(base_state)

    assert mock_llm.call_count == 0
    assert out["current_agent"] == "supervisor"
    assert out["needs_clarification"] is False


def test_graph_contains_intent_router_node(mock_llm, working_memory):
    graph = create_supervisor_graph(
        llm=mock_llm,
        working_memory=working_memory,
        long_term_memory=LongTermMemory(),
        enable_checkpointing=False,
    )
    nodes = set(graph.get_graph().nodes.keys())
    assert "intent_router" in nodes
    assert "supervisor_route" in nodes
    assert "knowledge_rag" in nodes
    assert "ticket_handler" in nodes


def test_graph_supervisor_route_edges_to_intent_router(mock_llm, working_memory):
    graph = create_supervisor_graph(
        llm=mock_llm,
        working_memory=working_memory,
        enable_checkpointing=False,
    )
    g = graph.get_graph()
    edge_pairs = {(e.source, e.target) for e in g.edges}
    assert ("supervisor_route", "intent_router") in edge_pairs


@pytest.mark.asyncio
async def test_full_graph_routes_to_ticket_handler(mock_llm, working_memory, base_state):
    graph = create_supervisor_graph(
        llm=mock_llm,
        working_memory=working_memory,
        long_term_memory=LongTermMemory(),
        enable_checkpointing=False,
    )
    result = await graph.ainvoke(base_state)

    assert result["intent"] == "ticket_handler"
    assert "intent_router" in result["sub_results"]
    assert result["sub_results"]["ticket_handler"]
    assert result["final_response"]
    assert result["compliance_passed"] is True
    ctx = working_memory.get_context("test-session")
    assert ctx.get("last_intent") == "ticket_handler"


@pytest.mark.asyncio
async def test_full_graph_knowledge_rag_path(working_memory, base_state, seeded_long_term_memory):
    llm = MockLLM(
        overrides={
            "intent_router": {
                "primary_intent": "consultation",
                "secondary_intent": "product_inquiry",
                "confidence": 0.95,
                "entities": {"product": "理财产品A"},
                "suggested_agent": "knowledge_rag",
            },
            "rag_answer": "理财产品A年化约3.5%-5.2%。",
        }
    )
    graph = create_supervisor_graph(
        llm=llm,
        working_memory=working_memory,
        long_term_memory=seeded_long_term_memory,
        enable_checkpointing=False,
    )
    base_state["messages"] = [HumanMessage(content="理财产品A收益多少？")]
    result = await graph.ainvoke(base_state)

    assert result["intent"] == "knowledge_rag"
    assert "knowledge_rag" in result["sub_results"]
    assert "理财产品" in result["final_response"] or "3.5" in result["final_response"]


@pytest.mark.asyncio
async def test_low_confidence_skips_sub_agent_and_clarifies(working_memory):
    llm = MockLLM(
        overrides={
            "intent_router": {
                "primary_intent": "unknown",
                "secondary_intent": "unknown",
                "confidence": 0.3,
                "entities": {},
                "suggested_agent": "knowledge_rag",
            }
        }
    )
    graph = create_supervisor_graph(
        llm=llm,
        working_memory=working_memory,
        long_term_memory=LongTermMemory(),
        enable_checkpointing=False,
    )
    state = {
        "messages": [HumanMessage(content="嗯")],
        "user_id": "u1",
        "session_id": "low-conf",
        "intent": "",
        "sub_results": {},
        "compliance_passed": True,
        "final_response": "",
        "current_agent": "",
        "retry_count": 0,
        "needs_clarification": False,
    }
    result = await graph.ainvoke(state)

    assert result["needs_clarification"] is True
    assert "不确定" in result["final_response"] or "补充" in result["final_response"]
    assert "knowledge_rag" not in result["sub_results"]
    assert "ticket_handler" not in result["sub_results"]


@pytest.mark.asyncio
async def test_synthesize_skips_intent_router_dict_in_output(working_memory):
    llm = MockLLM()
    supervisor = SupervisorNode(llm, working_memory)
    state = {
        "compliance_passed": True,
        "needs_clarification": False,
        "sub_results": {
            "intent_router": {"primary": "consultation", "confidence": 0.9},
            "knowledge_rag": "业务回答内容",
        },
    }
    out = await supervisor.synthesize_response(state)
    assert out["final_response"] == "业务回答内容"
    assert "consultation" not in out["final_response"]
