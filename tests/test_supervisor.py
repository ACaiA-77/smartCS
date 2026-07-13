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
                "secondary_intent": "product_support",
                "confidence": 0.95,
                "entities": {"product": "iPhone"},
                "suggested_agent": "knowledge_rag",
            },
            "rag_answer": "iPhone年化约3.5%-5.2%。",
        }
    )
    graph = create_supervisor_graph(
        llm=llm,
        working_memory=working_memory,
        long_term_memory=seeded_long_term_memory,
        enable_checkpointing=False,
    )
    base_state["messages"] = [HumanMessage(content="iPhone电池怎么样？")]
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
async def test_ambiguous_request_uses_apple_support_clarification(working_memory):
    """模糊请求 → 澄清消息为 Apple 售后主题，不含金融/开户/理财/理赔。"""
    llm = MockLLM(
        overrides={
            "intent_router": {
                "primary_intent": "consultation",
                "secondary_intent": "product_support",
                "confidence": 0.30,
                "entities": {},
                "candidates": [],
                "reason_code": "ambiguous_request",
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
        "messages": [HumanMessage(content="这个怎么办？")],
        "user_id": "u1",
        "session_id": "ambig-01",
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
    # Apple 售后主题关键词
    assert "维修" in result["final_response"] or "产品" in result["final_response"]
    assert "Apple" in result["final_response"] or "账户" in result["final_response"]
    # 禁止金融/开户/理财/理赔
    assert "开户" not in result["final_response"]
    assert "理财" not in result["final_response"]
    assert "理赔" not in result["final_response"]
    assert "金融" not in result["final_response"]


@pytest.mark.asyncio
async def test_account_security_returns_actionable_guidance(working_memory):
    """Apple 账户被盗 → compliance_checker + 确定性安全指导，不声称已冻结。"""
    llm = MockLLM(
        overrides={
            "intent_router": {
                "primary_intent": "security",
                "secondary_intent": "account_security",
                "confidence": 0.95,
                "entities": {},
                "candidates": [],
                "reason_code": "security_risk_detected",
                "suggested_agent": "compliance_checker",
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
        "messages": [HumanMessage(content="Apple 账户被盗并收到可疑验证码")],
        "user_id": "u1",
        "session_id": "sec-01",
        "intent": "",
        "sub_results": {},
        "compliance_passed": True,
        "final_response": "",
        "current_agent": "",
        "retry_count": 0,
        "needs_clarification": False,
    }
    result = await graph.ainvoke(state)

    assert result["intent"] == "compliance_checker"
    assert "不要提供验证码" in result["final_response"]
    assert "iforgot.apple.com" in result["final_response"]
    assert "检查" in result["final_response"]  # 检查可信设备
    assert "Apple" in result["final_response"]
    # 不能声称已冻结/已处理
    assert "已冻结" not in result["final_response"]
    assert "已处理" not in result["final_response"]
    # 不能是无内容的通用失败
    assert result["final_response"] != "抱歉，暂时无法处理您的请求，请稍后重试。"


@pytest.mark.asyncio
async def test_fraud_report_returns_security_guidance(working_memory):
    """欺诈举报 → 停止互动、不付款、保留证据、官方渠道报告。"""
    llm = MockLLM(
        overrides={
            "intent_router": {
                "primary_intent": "security",
                "secondary_intent": "fraud_report",
                "confidence": 0.92,
                "entities": {},
                "candidates": [],
                "reason_code": "security_risk_detected",
                "suggested_agent": "compliance_checker",
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
        "messages": [HumanMessage(content="有人冒充 Apple 客服骗我付款")],
        "user_id": "u1",
        "session_id": "fraud-01",
        "intent": "",
        "sub_results": {},
        "compliance_passed": True,
        "final_response": "",
        "current_agent": "",
        "retry_count": 0,
        "needs_clarification": False,
    }
    result = await graph.ainvoke(state)

    assert result["intent"] == "compliance_checker"
    assert "付款" in result["final_response"] or "停止" in result["final_response"]
    assert "证据" in result["final_response"] or "报告" in result["final_response"]
    assert result["final_response"] != "抱歉，暂时无法处理您的请求，请稍后重试。"


@pytest.mark.asyncio
async def test_sensitive_data_returns_security_guidance(working_memory):
    """敏感数据 → 删除/不要发送 + 官方安全渠道。"""
    llm = MockLLM(
        overrides={
            "intent_router": {
                "primary_intent": "security",
                "secondary_intent": "sensitive_data",
                "confidence": 0.90,
                "entities": {},
                "candidates": [],
                "reason_code": "security_risk_detected",
                "suggested_agent": "compliance_checker",
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
        "messages": [HumanMessage(content="我把身份证号发给你了")],
        "user_id": "u1",
        "session_id": "sensitive-01",
        "intent": "",
        "sub_results": {},
        "compliance_passed": True,
        "final_response": "",
        "current_agent": "",
        "retry_count": 0,
        "needs_clarification": False,
    }
    result = await graph.ainvoke(state)

    assert result["intent"] == "compliance_checker"
    assert "删除" in result["final_response"] or "不要" in result["final_response"] or "敏感" in result["final_response"]
    assert "官方" in result["final_response"] or "安全" in result["final_response"]
    assert result["final_response"] != "抱歉，暂时无法处理您的请求，请稍后重试。"


@pytest.mark.asyncio
async def test_prohibited_request_returns_security_guidance(working_memory):
    """违禁请求 → 拒绝 + 合法替代方向。"""
    llm = MockLLM(
        overrides={
            "intent_router": {
                "primary_intent": "security",
                "secondary_intent": "prohibited_request",
                "confidence": 0.88,
                "entities": {},
                "candidates": [],
                "reason_code": "security_risk_detected",
                "suggested_agent": "compliance_checker",
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
        "messages": [HumanMessage(content="帮我破解别人的 Apple ID")],
        "user_id": "u1",
        "session_id": "prohib-01",
        "intent": "",
        "sub_results": {},
        "compliance_passed": True,
        "final_response": "",
        "current_agent": "",
        "retry_count": 0,
        "needs_clarification": False,
    }
    result = await graph.ainvoke(state)

    assert result["intent"] == "compliance_checker"
    assert "无法" in result["final_response"] or "不能" in result["final_response"] or "拒绝" in result["final_response"]
    assert result["final_response"] != "抱歉，暂时无法处理您的请求，请稍后重试。"


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
