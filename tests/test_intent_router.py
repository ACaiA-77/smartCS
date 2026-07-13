"""IntentRouterAgent 单元测试 — Apple 售后 taxonomy（Task 2 审查修复）。

移除对实例级可变状态 _last_parse_mode 的依赖；
process() 直接写入顶层 needs_clarification；
新增候选差值阈值、并发安全、修复 prompt 隔离测试。
"""

from __future__ import annotations

import asyncio
import json

import pytest
from langchain_core.messages import HumanMessage

from agents.intent_models import (
    AgentTarget,
    IntentDecision,
    PrimaryIntent,
    SecondaryIntent,
)
from agents.intent_router import IntentRouterAgent
from tests.conftest import MockLLM, SequenceLLM

# ── 常量 ──────────────────────────────────────────────────────────

VALID_PRODUCT_SUPPORT_JSON = json.dumps(
    {
        "primary_intent": "consultation",
        "secondary_intent": "product_support",
        "confidence": 0.95,
        "entities": {"product": "AirPods"},
        "candidates": [],
        "reason_code": "explicit_policy_question",
        "suggested_agent": "knowledge_rag",
    },
    ensure_ascii=False,
)

# ── 已有测试（重写，移除 _last_parse_mode 依赖）───────────────────


@pytest.mark.asyncio
async def test_classify_parses_llm_json():
    """正常 JSON 解析通过 build_intent_decision 校验。"""
    llm = MockLLM(
        overrides={
            "intent_router": {
                "primary_intent": "consultation",
                "secondary_intent": "product_support",
                "confidence": 0.92,
                "entities": {"product": "iPhone"},
                "candidates": [],
                "reason_code": "explicit_policy_question",
                "suggested_agent": "knowledge_rag",
            }
        }
    )
    agent = IntentRouterAgent(llm)
    result = await agent.classify("iPhone 屏幕碎了怎么办？")

    assert isinstance(result, IntentDecision)
    assert result.primary_intent == PrimaryIntent.CONSULTATION
    assert result.secondary_intent == SecondaryIntent.PRODUCT_SUPPORT
    assert result.confidence == 0.92
    assert result.entities.product == "iPhone"
    assert result.suggested_agent == AgentTarget.KNOWLEDGE_RAG
    assert result.reason_code.value == "explicit_policy_question"
    # parse_mode 验证移入 process() 测试；classify() 不暴露实现细节


@pytest.mark.asyncio
async def test_classify_invalid_json_uses_rule_fallback():
    """无效 JSON → 格式修复失败 → 规则降级。"""
    llm = SequenceLLM(["not-json"])
    agent = IntentRouterAgent(llm)
    result = await agent.classify("hello")

    assert isinstance(result, IntentDecision)
    assert result.reason_code.value == "parser_fallback"
    assert result.confidence < 1.0


@pytest.mark.asyncio
async def test_process_writes_intent_and_sub_results():
    """process() 使用 Apple 默认 payload 正确写入 state。"""
    llm = MockLLM()
    agent = IntentRouterAgent(llm)
    state: dict = {
        "messages": [HumanMessage(content="查订单")],
        "sub_results": {},
    }
    out = await agent.process(state)

    assert out["intent"] == "ticket_handler"
    ir = out["sub_results"]["intent_router"]
    assert ir["primary"] == "query"
    assert ir["secondary"] == "order_query"
    assert ir["confidence"] == 0.95
    assert ir["entities"]["order_id"] == "ORD-001"
    assert "reason_code" in ir
    assert "parse_mode" in ir
    assert "needs_clarification" in ir
    assert "candidates" in ir
    # process() 必须同时写入顶层 needs_clarification
    assert "needs_clarification" in out
    assert out["needs_clarification"] == ir["needs_clarification"]


# ── Task 2 已有测试（重写，移除 _last_parse_mode 引用）─────────────


@pytest.mark.asyncio
async def test_policy_question_routes_to_rag_even_if_model_suggests_ticket():
    """模型建议 ticket_handler，但 subscription_policy → knowledge_rag（服务端确定性重算）。"""
    llm = MockLLM(
        overrides={
            "intent_router": {
                "primary_intent": "consultation",
                "secondary_intent": "subscription_policy",
                "confidence": 0.94,
                "entities": {"subscription": "AppleCare"},
                "candidates": [],
                "reason_code": "explicit_policy_question",
                "suggested_agent": "ticket_handler",
            }
        }
    )
    result = await IntentRouterAgent(llm).classify("AppleCare 可以取消吗？")
    assert result.suggested_agent.value == "knowledge_rag"


@pytest.mark.asyncio
async def test_invalid_json_repairs_once_then_validates():
    """首次 JSON 解析失败 → 格式修复成功 → call_count == 2。"""
    llm = SequenceLLM(["not-json", VALID_PRODUCT_SUPPORT_JSON])
    agent = IntentRouterAgent(llm)
    result = await agent.classify("如何清洁 AirPods？")
    assert llm.call_count == 2
    assert result.secondary_intent == SecondaryIntent.PRODUCT_SUPPORT


@pytest.mark.asyncio
async def test_invalid_json_twice_uses_security_fallback():
    """两次 JSON 解析均失败 → 规则降级 → 安全类消息路由到 compliance_checker。"""
    llm = SequenceLLM(["bad", "still bad"])
    agent = IntentRouterAgent(llm)
    result = await agent.classify("Apple 账户被盗并收到验证码")
    assert result.suggested_agent.value == "compliance_checker"
    assert llm.call_count == 2


@pytest.mark.asyncio
async def test_validation_error_counts_as_format_failure():
    """Pydantic ValidationError 也算格式失败，触发修复/降级流程。"""
    bad_payload = json.dumps(
        {
            "primary_intent": "consultation",
            "secondary_intent": "product_support",
            "confidence": 0.9,
            "entities": {"product": "iPhone"},
            "candidates": [],
            "reason_code": "explicit_policy_question",
            "suggested_agent": "invalid_agent",
        },
        ensure_ascii=False,
    )
    llm = SequenceLLM([bad_payload, VALID_PRODUCT_SUPPORT_JSON])
    agent = IntentRouterAgent(llm)
    result = await agent.classify("iPhone 如何备份？")
    assert result.suggested_agent == AgentTarget.KNOWLEDGE_RAG
    assert llm.call_count == 2


@pytest.mark.asyncio
async def test_rule_fallback_security_detected():
    """规则降级：安全关键词 → compliance_checker。"""
    llm = SequenceLLM(["garbage %%@"])
    agent = IntentRouterAgent(llm)
    result = await agent.classify("我的 Apple ID 被盗了有人远程登录")
    assert result.suggested_agent.value == "compliance_checker"
    assert result.primary_intent == PrimaryIntent.SECURITY


@pytest.mark.asyncio
async def test_rule_fallback_order_query():
    """规则降级：订单/工单号 → ticket_handler。"""
    llm = SequenceLLM(["~~~"])
    agent = IntentRouterAgent(llm)
    result = await agent.classify("查询订单 ORD-88888 的状态")
    assert result.suggested_agent.value == "ticket_handler"
    assert result.primary_intent == PrimaryIntent.QUERY


@pytest.mark.asyncio
async def test_rule_fallback_explicit_action():
    """规则降级：明确动作关键词（退款/维修）→ ticket_handler。"""
    llm = SequenceLLM(["???"])
    agent = IntentRouterAgent(llm)
    result = await agent.classify("我要申请退款")
    assert result.suggested_agent.value == "ticket_handler"
    assert result.primary_intent == PrimaryIntent.ACTION


@pytest.mark.asyncio
async def test_rule_fallback_consultation():
    """规则降级：普通咨询 → knowledge_rag。"""
    llm = SequenceLLM(["---"])
    agent = IntentRouterAgent(llm)
    result = await agent.classify("iPhone 15 有什么新功能？")
    assert result.suggested_agent.value == "knowledge_rag"
    assert result.primary_intent == PrimaryIntent.CONSULTATION


@pytest.mark.asyncio
async def test_rule_fallback_unknown():
    """规则降级：无法识别 → 低置信度（< 0.7），回退 knowledge_rag。"""
    llm = SequenceLLM(["..."])
    agent = IntentRouterAgent(llm)
    result = await agent.classify("嗯")
    assert result.confidence < 0.7
    assert result.suggested_agent.value == "knowledge_rag"


# ═══════════════════════════════════════════════════════════════════════
# Task 2 审查修复新增测试
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_same_agent_consecutive_calls_no_cross_contamination():
    """同一 Agent 连续 direct → fallback 不污染：两次 classify() 各自独立。"""
    # 第一次：合法 JSON → direct parse
    llm_direct = MockLLM(
        overrides={
            "intent_router": {
                "primary_intent": "consultation",
                "secondary_intent": "product_support",
                "confidence": 0.92,
                "entities": {},
                "candidates": [],
                "reason_code": "explicit_policy_question",
                "suggested_agent": "knowledge_rag",
            }
        }
    )
    agent = IntentRouterAgent(llm_direct)
    result1 = await agent.classify("iPhone 屏幕碎了")
    assert result1.primary_intent == PrimaryIntent.CONSULTATION

    # 第二次：用新的 SequenceLLM 替换 → 应触发 rule_fallback
    # 关键：不能沿用第一次的 parse_mode
    agent2 = IntentRouterAgent(SequenceLLM(["???"]))
    result2 = await agent2.classify("查询订单 ORD-88888 的状态")
    # 因为 fallback 先检查安全（无）、订单ID匹配 → ticket_handler
    assert result2.reason_code.value == "parser_fallback"
    assert result2.primary_intent == PrimaryIntent.QUERY

    # 同一 agent 实例连续调用不同路径
    llm_seq = SequenceLLM([VALID_PRODUCT_SUPPORT_JSON, "garbage"])
    agent3 = IntentRouterAgent(llm_seq)

    # 首次：direct_parse
    r1 = await agent3.classify("AirPods 怎么连接")
    assert r1.secondary_intent == SecondaryIntent.PRODUCT_SUPPORT
    assert r1.reason_code.value == "explicit_policy_question"

    # 二次：fallback（"garbage" JSON 不可解析，无安全关键词，咨询关键词"什么"匹配）
    r2 = await agent3.classify("这是什么")
    assert r2.reason_code.value == "parser_fallback"
    # 两次调用独立，r1 不受 r2 影响
    assert r1.secondary_intent == SecondaryIntent.PRODUCT_SUPPORT


@pytest.mark.asyncio
async def test_concurrent_processes_independent():
    """asyncio.gather 并发两个 process() 不串 parse_mode。"""
    llm1 = MockLLM(
        overrides={
            "intent_router": {
                "primary_intent": "consultation",
                "secondary_intent": "product_support",
                "confidence": 0.95,
                "entities": {},
                "candidates": [],
                "reason_code": "explicit_policy_question",
                "suggested_agent": "knowledge_rag",
            }
        }
    )
    # 第二个用 SequenceLLM → 应触发 rule_fallback
    llm2 = SequenceLLM(["not-valid-json-at-all"])

    agent1 = IntentRouterAgent(llm1)
    agent2 = IntentRouterAgent(llm2)

    state1 = {
        "messages": [HumanMessage(content="如何清洁AirPods")],
        "sub_results": {},
    }
    state2 = {
        "messages": [HumanMessage(content="查询订单 ORD-88888 的状态")],
        "sub_results": {},
    }

    r1, r2 = await asyncio.gather(
        agent1.process(state1),
        agent2.process(state2),
    )

    # agent1: direct parse → knowledge_rag
    ir1 = r1["sub_results"]["intent_router"]
    assert ir1["parse_mode"] == "direct_parse"
    assert ir1["primary"] == "consultation"
    assert r1["needs_clarification"] is False

    # agent2: fallback → ticket_handler (订单ID)
    ir2 = r2["sub_results"]["intent_router"]
    assert ir2["parse_mode"] == "rule_fallback"
    assert ir2["primary"] == "query"
    # rule_fallback for order → confidence 0.80 < 0.7? No, 0.80 >= 0.7 → not needs_clarification
    # But wait, with default threshold 0.70, 0.80 >= 0.70, so no clarification
    assert r2["needs_clarification"] is False


@pytest.mark.asyncio
async def test_repair_prompt_excludes_original_user_message():
    """格式修复 prompt 包含坏输出但不应包含原始用户消息。"""
    bad_output = '{"primary_intent": "consultation", missing_quote: bad}'
    user_message = "我的信用卡被盗刷了怎么办"  # 敏感用户消息

    llm = SequenceLLM([bad_output, VALID_PRODUCT_SUPPORT_JSON])
    agent = IntentRouterAgent(llm)
    await agent.classify(user_message)

    assert llm.call_count == 2
    # 第二次调用是格式修复；检查修复 prompt
    assert len(llm.call_log) == 2
    repair_system, repair_human = llm.call_log[1]

    # 修复 prompt 应包含修复指令（不含原始用户消息）
    assert "格式" in repair_system or "JSON" in repair_system or "schema" in repair_system.lower()
    if repair_human:
        # 修复 human prompt 不应包含原始敏感用户消息
        assert "信用卡" not in repair_human
        assert "盗刷" not in repair_human


@pytest.mark.asyncio
async def test_validation_error_also_repaired_once():
    """Pydantic ValidationError（如非法 agent 枚举值）触发一次修复。"""
    bad_payload = json.dumps(
        {
            "primary_intent": "consultation",
            "secondary_intent": "product_support",
            "confidence": 0.9,
            "entities": {},
            "candidates": [],
            "reason_code": "explicit_policy_question",
            "suggested_agent": "INVALID_AGENT_NAME",
        },
        ensure_ascii=False,
    )
    llm = SequenceLLM([bad_payload, VALID_PRODUCT_SUPPORT_JSON])
    agent = IntentRouterAgent(llm)
    result = await agent.classify("iPhone 电池怎么换")
    assert result.suggested_agent == AgentTarget.KNOWLEDGE_RAG
    assert llm.call_count == 2


@pytest.mark.asyncio
async def test_candidate_margin_0_14_clarifies():
    """前两项意图置信度差 0.14（< 0.15 margin）→ needs_clarification。"""
    payload = json.dumps(
        {
            "primary_intent": "consultation",
            "secondary_intent": "product_support",
            "confidence": 0.85,
            "entities": {},
            "candidates": [
                {
                    "primary_intent": "action",
                    "secondary_intent": "refund_request",
                    "confidence": 0.71,
                }
            ],
            "reason_code": "explicit_policy_question",
            "suggested_agent": "knowledge_rag",
        },
        ensure_ascii=False,
    )
    llm = MockLLM(
        overrides={
            "intent_router": json.loads(payload),
        }
    )
    agent = IntentRouterAgent(llm)
    state = {
        "messages": [HumanMessage(content="iPhone 退款")],
        "sub_results": {},
    }
    out = await agent.process(state)

    ir = out["sub_results"]["intent_router"]
    # action (refund_request) 优先级高于 consultation，候选胜出
    assert ir["primary"] == "action"
    assert ir["secondary"] == "refund_request"
    # 主意图 confidence 0.71，原主意图降级为候选 confidence 0.85
    # diff = 0.85 - 0.71 = 0.14 < 0.15 → needs_clarification
    assert out["needs_clarification"] is True
    assert ir["needs_clarification"] is True


@pytest.mark.asyncio
async def test_candidate_margin_0_15_does_not_clarify():
    """前两项意图置信度差 0.15（== margin）→ 不触发 needs_clarification。"""
    payload = json.dumps(
        {
            "primary_intent": "consultation",
            "secondary_intent": "product_support",
            "confidence": 0.85,
            "entities": {},
            "candidates": [
                {
                    "primary_intent": "action",
                    "secondary_intent": "refund_request",
                    "confidence": 0.70,
                }
            ],
            "reason_code": "explicit_policy_question",
            "suggested_agent": "knowledge_rag",
        },
        ensure_ascii=False,
    )
    llm = MockLLM(
        overrides={
            "intent_router": json.loads(payload),
        }
    )
    agent = IntentRouterAgent(llm)
    state = {
        "messages": [HumanMessage(content="iPhone 退款")],
        "sub_results": {},
    }
    out = await agent.process(state)

    ir = out["sub_results"]["intent_router"]
    # action 胜出，confidence 0.70
    assert ir["primary"] == "action"
    # diff = 0.85 - 0.70 = 0.15 >= 0.15 → no clarification (but confidence 0.70 >= 0.70)
    # wait: confidence 0.70 == threshold → not < threshold, so not triggered by low confidence either
    assert out["needs_clarification"] is False
    assert ir["needs_clarification"] is False


@pytest.mark.asyncio
async def test_confidence_below_threshold_clarifies():
    """置信度低于阈值（0.69 < 0.70）→ needs_clarification。"""
    payload = json.dumps(
        {
            "primary_intent": "consultation",
            "secondary_intent": "product_support",
            "confidence": 0.69,
            "entities": {},
            "candidates": [],
            "reason_code": "ambiguous_request",
            "suggested_agent": "knowledge_rag",
        },
        ensure_ascii=False,
    )
    llm = MockLLM(
        overrides={
            "intent_router": json.loads(payload),
        }
    )
    agent = IntentRouterAgent(llm)
    state = {
        "messages": [HumanMessage(content="帮我看看")],
        "sub_results": {},
    }
    out = await agent.process(state)

    assert out["needs_clarification"] is True
    assert out["sub_results"]["intent_router"]["needs_clarification"] is True


@pytest.mark.asyncio
async def test_process_top_level_and_sub_results_consistent():
    """process() 顶层 needs_clarification 与 sub_results 中完全一致。"""
    llm = MockLLM(
        overrides={
            "intent_router": {
                "primary_intent": "consultation",
                "secondary_intent": "product_support",
                "confidence": 0.55,
                "entities": {},
                "candidates": [],
                "reason_code": "ambiguous_request",
                "suggested_agent": "knowledge_rag",
            }
        }
    )
    agent = IntentRouterAgent(llm)
    state = {
        "messages": [HumanMessage(content="不知道")],
        "sub_results": {},
    }
    out = await agent.process(state)

    ir = out["sub_results"]["intent_router"]
    assert "needs_clarification" in out
    assert "needs_clarification" in ir
    assert out["needs_clarification"] == ir["needs_clarification"]
    assert out["needs_clarification"] is True


@pytest.mark.asyncio
async def test_unknown_fallback_uses_product_support_low_confidence():
    """未知 fallback：product_support + 低 confidence 是 UNKNOWN secondary 缺失下的兼容策略。"""
    llm = SequenceLLM(["..."])
    agent = IntentRouterAgent(llm)
    result = await agent.classify("嗯")

    assert result.secondary_intent == SecondaryIntent.PRODUCT_SUPPORT
    assert result.confidence == 0.30
    assert result.reason_code.value == "parser_fallback"
    # 当前无 SecondaryIntent.UNKNOWN，product_support + 低置信度为兼容策略


@pytest.mark.asyncio
async def test_init_accepts_custom_thresholds():
    """__init__ 接受 confidence_threshold、candidate_margin、enable_format_repair。"""
    from langchain_openai import ChatOpenAI

    # 使用最小构造验证参数被接受
    llm = MockLLM()
    agent = IntentRouterAgent(
        llm,
        confidence_threshold=0.60,
        candidate_margin=0.20,
        enable_format_repair=False,
    )
    assert agent.confidence_threshold == 0.60
    assert agent.candidate_margin == 0.20
    assert agent.enable_format_repair is False

@pytest.mark.asyncio
async def test_init_accepts_context_turns_and_prompt_version():
    agent = IntentRouterAgent(
        MockLLM(),
        context_turns=4,
        prompt_version="apple-support-v2",
    )

    assert agent.context_turns == 4
    assert agent.prompt_version == "apple-support-v2"

@pytest.mark.asyncio
async def test_rule_fallback_treats_applecare_cancellation_question_as_policy_consultation():
    agent = IntentRouterAgent(SequenceLLM(["not-json"]))

    result = await agent.classify("AppleCare 可以取消吗？")

    assert result.secondary_intent is SecondaryIntent.SUBSCRIPTION_POLICY
    assert result.suggested_agent is AgentTarget.KNOWLEDGE_RAG

@pytest.mark.asyncio
async def test_rule_fallback_treats_explicit_applecare_cancellation_as_ticket_action():
    agent = IntentRouterAgent(SequenceLLM(["not-json"]))

    result = await agent.classify("帮我取消 AppleCare")

    assert result.secondary_intent is SecondaryIntent.SUBSCRIPTION_CANCEL_REQUEST
    assert result.suggested_agent is AgentTarget.TICKET_HANDLER
