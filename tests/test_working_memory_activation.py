"""Working Memory 激活测试（Mock LLM，无外部依赖）。"""

from __future__ import annotations

import pytest
from langchain_core.messages import HumanMessage

from agents.supervisor import SupervisorNode, create_supervisor_graph
from memory.long_term import LongTermMemory
from memory.working_memory import WorkingMemory
from tests.conftest import MockLLM


@pytest.mark.asyncio
async def test_route_decision_injects_wm_context(mock_llm, working_memory, base_state):
    """route_decision 应将工作记忆内容注入 sub_results['_wm_context']。"""
    working_memory.update("test-session", {"last_intent": "ticket_handler"})

    supervisor = SupervisorNode(mock_llm, working_memory)
    out = await supervisor.route_decision(base_state)

    wm = out.get("sub_results", {}).get("_wm_context", {})
    assert wm.get("last_intent") == "ticket_handler"
    assert "accumulated_entities" in wm
    assert "turn_count" in wm


@pytest.mark.asyncio
async def test_intent_router_node_accumulates_entities(mock_llm, working_memory, base_state):
    """intent_router_node 应将本轮实体合并到工作记忆的 accumulated_entities。"""
    working_memory.update("test-session", {
        "accumulated_entities": {"product": "理财产品A"},
        "turn_count": 1,
    })

    graph = create_supervisor_graph(
        llm=mock_llm,
        working_memory=working_memory,
        long_term_memory=LongTermMemory(),
        enable_checkpointing=False,
    )
    result = await graph.ainvoke(base_state)

    ctx = working_memory.get_context("test-session")
    assert "accumulated_entities" in ctx
    assert ctx.get("turn_count", 0) >= 2
    wm = result.get("sub_results", {}).get("_wm_context", {})
    assert "accumulated_entities" in wm


@pytest.mark.asyncio
async def test_classify_accepts_last_intent_param(mock_llm):
    """classify 应接受 last_intent 参数且不报错。"""
    from agents.intent_router import IntentRouterAgent

    router = IntentRouterAgent(mock_llm)
    result = await router.classify(
        user_message="查询那个订单",
        chat_context="",
        last_intent="ticket_handler",
    )
    assert result is not None
    assert result.primary_intent is not None


@pytest.mark.asyncio
async def test_knowledge_rag_uses_accumulated_entities(working_memory, base_state, seeded_long_term_memory):
    """knowledge_rag 应从 _wm_context.accumulated_entities 读取实体补全 query。"""
    llm = MockLLM(
        overrides={
            "intent_router": {
                "primary_intent": "consultation",
                "secondary_intent": "product_inquiry",
                "confidence": 0.95,
                "entities": {},
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

    working_memory.update("test-session", {
        "accumulated_entities": {"product": "理财产品A"},
        "turn_count": 2,
    })

    base_state["messages"] = [HumanMessage(content="收益率多少？")]
    result = await graph.ainvoke(base_state)

    assert "knowledge_rag" in result["sub_results"]
    assert result["final_response"]


@pytest.mark.asyncio
async def test_ticket_handler_uses_accumulated_entities(working_memory):
    """ticket_handler 应从 _wm_context.accumulated_entities 回退读取实体。"""
    llm = MockLLM(
        overrides={
            "intent_router": {
                "primary_intent": "transaction",
                "secondary_intent": "order_query",
                "confidence": 0.9,
                "entities": {},
                "suggested_agent": "ticket_handler",
            },
        }
    )
    graph = create_supervisor_graph(
        llm=llm,
        working_memory=working_memory,
        long_term_memory=LongTermMemory(),
        enable_checkpointing=False,
    )

    working_memory.update("test-session", {
        "accumulated_entities": {"order_id": "ORD-20260621-001"},
        "turn_count": 2,
    })

    state = {
        "messages": [HumanMessage(content="查一下状态")],
        "user_id": "u1",
        "session_id": "test-session",
        "intent": "",
        "sub_results": {},
        "compliance_passed": True,
        "final_response": "",
        "current_agent": "",
        "retry_count": 0,
        "needs_clarification": False,
    }
    result = await graph.ainvoke(state)

    assert result["final_response"]


@pytest.mark.asyncio
async def test_export_for_persistence_exports_context():
    """export_for_persistence 应返回包含 last_intent 和 accumulated_entities 的上下文。"""
    wm = WorkingMemory()
    wm.update("persist-test", {
        "last_intent": "knowledge_rag",
        "accumulated_entities": {"product": "理财产品A"},
        "turn_count": 1,
    })

    exported = wm.export_for_persistence("persist-test")
    assert exported["context"]["last_intent"] == "knowledge_rag"
    assert exported["context"]["accumulated_entities"]["product"] == "理财产品A"


@pytest.mark.asyncio
async def test_synthesize_excludes_wm_context_from_output(working_memory):
    """synthesize_response 应跳过 _wm_context，不将其拼入 final_response。"""
    llm = MockLLM()
    supervisor = SupervisorNode(llm, working_memory)
    state = {
        "compliance_passed": True,
        "needs_clarification": False,
        "sub_results": {
            "intent_router": {"primary": "consultation", "confidence": 0.9},
            "_wm_context": {"last_intent": "knowledge_rag", "accumulated_entities": {"product": "A"}},
            "knowledge_rag": "理财产品A年化约3.5%-5.2%。",
        },
    }
    out = await supervisor.synthesize_response(state)
    assert "last_intent" not in out["final_response"]
    assert "accumulated_entities" not in out["final_response"]
    assert "理财产品" in out["final_response"]


@pytest.mark.asyncio
async def test_multi_turn_entity_accumulation_e2e(working_memory, seeded_long_term_memory):
    """模拟两轮对话：第一轮提取 product 实体，第二轮 query 应能用上累积实体。"""
    # --- Turn 1: 用户提到产品名 ---
    llm1 = MockLLM(
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
    graph1 = create_supervisor_graph(
        llm=llm1,
        working_memory=working_memory,
        long_term_memory=seeded_long_term_memory,
        enable_checkpointing=False,
    )
    state1 = {
        "messages": [HumanMessage(content="理财产品A收益多少？")],
        "user_id": "u1",
        "session_id": "e2e-session",
        "intent": "",
        "sub_results": {},
        "compliance_passed": True,
        "final_response": "",
        "current_agent": "",
        "retry_count": 0,
        "needs_clarification": False,
    }
    result1 = await graph1.ainvoke(state1)
    assert result1["final_response"]

    # 验证工作记忆已累积实体
    ctx = working_memory.get_context("e2e-session")
    assert ctx.get("accumulated_entities", {}).get("product") == "理财产品A"
    assert ctx.get("turn_count") == 1
    assert ctx.get("last_intent") == "knowledge_rag"

    # --- Turn 2: 用户省略产品名 ---
    llm2 = MockLLM(
        overrides={
            "intent_router": {
                "primary_intent": "consultation",
                "secondary_intent": "rate_inquiry",
                "confidence": 0.9,
                "entities": {},
                "suggested_agent": "knowledge_rag",
            },
            "rag_answer": "理财产品A最新利率信息。",
        }
    )
    graph2 = create_supervisor_graph(
        llm=llm2,
        working_memory=working_memory,
        long_term_memory=seeded_long_term_memory,
        enable_checkpointing=False,
    )
    state2 = {
        "messages": [HumanMessage(content="最新利率是多少？")],
        "user_id": "u1",
        "session_id": "e2e-session",
        "intent": "",
        "sub_results": {},
        "compliance_passed": True,
        "final_response": "",
        "current_agent": "",
        "retry_count": 0,
        "needs_clarification": False,
    }
    result2 = await graph2.ainvoke(state2)

    # 第二轮应成功路由并生成回复
    assert result2["final_response"]
    assert result2["intent"] == "knowledge_rag"

    # 工作记忆 turn_count 应递增
    ctx2 = working_memory.get_context("e2e-session")
    assert ctx2.get("turn_count") == 2
