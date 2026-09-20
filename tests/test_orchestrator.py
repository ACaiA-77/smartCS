"""Contract tests for the explicit production chat orchestrator."""

from __future__ import annotations

import inspect
import json
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from langchain_core.messages import HumanMessage

from agents.orchestrator import ChatOrchestrator, create_chat_orchestrator
from memory.long_term import HashEmbeddingBackend, LongTermMemory
from memory.session_store import SessionStore
from memory.short_term import ShortTermMemory
from mcp.approval_store import ApprovalService
from mcp.execution_ledger import ExecutionLedger
from mcp.mcp_server import MCPToolServer, create_default_tools
from mcp.order_repository import OrderRepository
from mcp.tool_execution import ToolExecutor
from tests.conftest import MockLLM


def _state(message: str, *, user_id: str = "user_002", session_id: str = "session-1") -> dict:
    return {
        "messages": [HumanMessage(content=message)],
        "user_id": user_id,
        "session_id": session_id,
        "intent": "",
        "sub_results": {},
        "compliance_passed": True,
        "final_response": "",
        "current_agent": "",
        "retry_count": 0,
        "needs_clarification": False,
    }


def _services(tmp_path, *, overrides=None):
    repository = OrderRepository(str(tmp_path / "orders.db"))
    long_term = LongTermMemory()
    server = create_default_tools(
        MCPToolServer(), long_term_memory=long_term, order_repository=repository
    )
    ledger = ExecutionLedger(repository.db_path)
    approvals = ApprovalService(repository.db_path)
    executor = ToolExecutor(server, ledger=ledger, approval_service=approvals)
    llm = MockLLM(overrides=overrides)
    short = ShortTermMemory(redis_url="redis://127.0.0.1:6399/0", redis_retry_cooldown=60)
    session_store = SessionStore(short)
    return llm, session_store, short, long_term, server, executor, repository


def _orchestrator(tmp_path, *, overrides=None):
    llm, session_store, short, long_term, server, executor, repository = _services(
        tmp_path, overrides=overrides
    )
    return (
        create_chat_orchestrator(
            llm=llm,
            session_store=session_store,
            long_term_memory=long_term,
            mcp_server=server,
            tool_executor=executor,
        ),
        session_store,
        short,
        executor,
        repository,
    )


@pytest.mark.asyncio
async def test_low_confidence_skips_business_handler_but_runs_compliance(tmp_path):
    overrides = {
        "intent_router": {
            "primary_intent": "unknown",
            "secondary_intent": "unknown",
            "confidence": 0.3,
            "entities": {},
            "suggested_agent": "ticket_handler",
        },
        "ticket_handler": {
            "action": "create",
            "ticket_type": "complaint",
            "priority": "medium",
            "summary": "服务投诉",
            "details": "用户请求处理投诉",
        },
    }
    orchestrator, _, _, executor, _ = _orchestrator(tmp_path, overrides=overrides)

    result = await orchestrator.ainvoke(_state("嗯"))

    assert result["needs_clarification"] is True
    assert "不太确定" in result["final_response"]
    assert "ticket_handler" not in result["sub_results"]
    assert "refund_handler" not in result["sub_results"]
    assert result["compliance_passed"] is True
    assert executor.ledger is not None


@pytest.mark.asyncio
async def test_refund_two_turn_request_confirm_and_cancel(tmp_path):
    orchestrator, session_store, _, _, repository = _orchestrator(tmp_path)
    first = await orchestrator.ainvoke(_state("帮我退款 ORD-20260801-0002"))

    assert first["intent"] == "refund_handler"
    assert first["sub_results"]["refund_handler"]
    assert (await session_store.get_state("session-1")).pending_action
    assert repository.get_order("ORD-20260801-0002")["refunds"] == []

    confirmed = await orchestrator.ainvoke(_state("确认退款"))
    assert "退款申请已提交" in confirmed["final_response"]
    assert (await session_store.get_state("session-1")).pending_action is None
    assert len(repository.get_order("ORD-20260801-0002")["refunds"]) == 1

    cancelled_orchestrator, cancelled_store, _, _, cancelled_repo = _orchestrator(
        tmp_path / "cancel"
    )
    await cancelled_orchestrator.ainvoke(_state("帮我退款 ORD-20260801-0002"))
    cancelled = await cancelled_orchestrator.ainvoke(_state("取消退款"))
    assert "已取消" in cancelled["final_response"]
    assert (await cancelled_store.get_state("session-1")).pending_action is None
    assert cancelled_repo.get_order("ORD-20260801-0002")["refunds"] == []


@pytest.mark.asyncio
async def test_ticket_consent_is_preserved_in_orchestrator(tmp_path):
    overrides = {
        "intent_router": {
            "primary_intent": "complaint",
            "secondary_intent": "complaint",
            "confidence": 0.95,
            "entities": {},
            "suggested_agent": "ticket_handler",
        },
        "ticket_handler": {
            "action": "create",
            "ticket_type": "complaint",
            "priority": "medium",
            "summary": "服务投诉",
            "details": "用户请求处理投诉",
        },
    }
    orchestrator, _, _, executor, _ = _orchestrator(tmp_path, overrides=overrides)

    consultation = await orchestrator.ainvoke(_state("投诉流程是什么？"))
    assert "工单已创建成功" not in consultation["final_response"]
    assert executor.ledger.get("compliance-escalation:session-1") is None

    explicit = await orchestrator.ainvoke(_state("我要投诉服务问题"))
    assert "工单已创建成功" in explicit["final_response"]


@pytest.mark.asyncio
async def test_compliance_failure_escalates_through_tool_executor(tmp_path):
    overrides = {
        "compliance": {
            "passed": False,
            "risk_level": "high",
            "violations": ["敏感内容"],
            "suggestions": [],
        }
    }
    orchestrator, _, _, executor, _ = _orchestrator(tmp_path, overrides=overrides)

    result = await orchestrator.ainvoke(_state("普通咨询"))

    assert result["compliance_passed"] is False
    assert "转交人工客服" in result["final_response"]
    record = executor.ledger.get("compliance-escalation:session-1")
    assert record is not None and record["status"] == "completed"


@pytest.mark.asyncio
async def test_snapshot_restore_recovers_pending_refund(tmp_path):
    orchestrator, session_store, short, _, repository = _orchestrator(tmp_path)
    await orchestrator.ainvoke(_state("帮我退款 ORD-20260801-0002"))
    snapshot = (await session_store.get_state("session-1")).to_dict()
    await short.delete_value(session_store._state_key("session-1"))
    await short.add_message("session-1", "system", f"[wm_snapshot]{json.dumps(snapshot, ensure_ascii=False)}")

    restarted_store = SessionStore(short)
    restarted = create_chat_orchestrator(
        llm=orchestrator.llm,
        session_store=restarted_store,
        long_term_memory=orchestrator.long_term_memory,
        mcp_server=orchestrator.mcp_server,
        tool_executor=orchestrator.tool_executor,
    )

    result = await restarted.ainvoke(_state("确认退款"))

    assert "退款申请已提交" in result["final_response"]
    assert repository.get_order("ORD-20260801-0002")["refunds"]


@pytest.mark.asyncio
async def test_conversation_skips_retrieval_and_business_tools(tmp_path):
    orchestrator, session_store, _, executor, _ = _orchestrator(
        tmp_path,
        overrides={
            "intent_router": {
                "primary_intent": "conversation",
                "secondary_intent": "identity",
                "confidence": 0.99,
                "entities": {},
                "suggested_agent": "conversation",
            },
            "default": "你好，我是 SmartCS 智能客服助手。",
        },
    )
    orchestrator.knowledge_agent.process = AsyncMock(side_effect=AssertionError("unexpected RAG"))
    orchestrator.long_term_memory.search = Mock(side_effect=AssertionError("unexpected retrieval"))
    executor.execute = AsyncMock(side_effect=AssertionError("unexpected business tool"))

    result = await orchestrator.ainvoke(_state("你好，请问你是谁？"))

    assert result["intent"] == "conversation"
    assert result["final_response"] == "你好，我是 SmartCS 智能客服助手。"
    assert result["compliance_passed"] is True
    assert result["sub_results"]["compliance"]["passed"] is True
    assert (await session_store.get_state("session-1")).last_intent == "conversation"
    orchestrator.knowledge_agent.process.assert_not_called()
    orchestrator.long_term_memory.search.assert_not_called()
    executor.execute.assert_not_called()
    assert orchestrator.llm.call_count == 3  # Router, conversation, compliance; no RAG calls.


@pytest.mark.asyncio
async def test_unknown_high_confidence_route_clarifies_without_retrieval(tmp_path):
    orchestrator, _, _, executor, _ = _orchestrator(
        tmp_path,
        overrides={"intent_router": {
            "primary_intent": "unknown", "secondary_intent": "unknown",
            "confidence": 0.99, "entities": {}, "suggested_agent": "unsupported_agent",
        }},
    )
    orchestrator.knowledge_agent.process = AsyncMock(side_effect=AssertionError("unexpected RAG"))
    executor.execute = AsyncMock(side_effect=AssertionError("unexpected business tool"))

    result = await orchestrator.ainvoke(_state("嗯，这个呢？"))

    assert result["needs_clarification"] is True
    assert "不太确定" in result["final_response"]
    orchestrator.knowledge_agent.process.assert_not_called()
    executor.execute.assert_not_called()


@pytest.mark.asyncio
async def test_thanks_does_not_confirm_or_clear_pending_refund(tmp_path):
    orchestrator, session_store, _, executor, repository = _orchestrator(tmp_path)
    await orchestrator.ainvoke(_state("帮我退款 ORD-20260801-0002"))
    pending = (await session_store.get_state("session-1")).pending_action
    assert pending
    orchestrator.llm.overrides["intent_router"] = {
        "primary_intent": "conversation", "secondary_intent": "gratitude",
        "confidence": 0.98, "entities": {}, "suggested_agent": "conversation",
    }
    execute = executor.execute
    executor.execute = AsyncMock(side_effect=AssertionError("thanks must not execute a tool"))

    result = await orchestrator.ainvoke(_state("谢谢你"))

    assert result["intent"] == "conversation"
    assert (await session_store.get_state("session-1")).pending_action == pending
    assert repository.get_order("ORD-20260801-0002")["refunds"] == []
    executor.execute.assert_not_called()

    executor.execute = execute
    confirmed = await orchestrator.ainvoke(_state("确认退款"))
    assert "退款申请已提交" in confirmed["final_response"]
    assert len(repository.get_order("ORD-20260801-0002")["refunds"]) == 1


@pytest.mark.parametrize("temperature, expected", [(None, 0.0), ("1", 1.0)])
def test_model_temperature_configuration(tmp_path, monkeypatch, temperature, expected):
    llm, session_store, _, long_term, _, _, _ = _services(tmp_path)
    factory = Mock(return_value=llm)
    monkeypatch.setattr("agents.orchestrator.create_traced_chat_openai", factory)
    monkeypatch.setenv("MODEL_NAME", "kimi-k2.7-code")
    if temperature is None:
        monkeypatch.delenv("MODEL_TEMPERATURE", raising=False)
    else:
        monkeypatch.setenv("MODEL_TEMPERATURE", temperature)

    result = create_chat_orchestrator(session_store=session_store, long_term_memory=long_term)

    factory.assert_called_once_with(model="kimi-k2.7-code", temperature=expected)
    assert result.llm is llm


def test_production_uses_explicit_orchestrator():
    source = Path("agents/orchestrator.py").read_text(encoding="utf-8")
    api_source = Path("api/main.py").read_text(encoding="utf-8")

    assert inspect.iscoroutinefunction(ChatOrchestrator.ainvoke)
    assert "lang" + "graph" not in source.lower()
    assert "create_" + "supervisor_graph(" not in api_source
    assert "graph." + "ainvoke" not in api_source
    assert "chat_orchestrator.ainvoke" in api_source


def test_orchestrator_uses_explicit_memory_when_production_rag_root_is_set(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("RAG_INDEX_ROOT", str(tmp_path / "production-index"))
    memory = LongTermMemory(
        index_path=str(tmp_path / "vectors"),
        embedding_backend=HashEmbeddingBackend(64),
    )
    memory.add_document("隔离知识库中的账户恢复说明。", "isolated.md")
    short = ShortTermMemory(redis_url="redis://127.0.0.1:6399/0", redis_retry_cooldown=60)
    orchestrator = ChatOrchestrator(MockLLM(), SessionStore(short), memory)

    hits = orchestrator.retriever.retrieve("账户恢复说明", top_k=1, rerank=False)

    assert hits[0].source == "isolated.md"
    assert orchestrator.retriever.legacy_memory is memory
