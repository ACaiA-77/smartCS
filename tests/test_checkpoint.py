"""Checkpoint contracts; real MySQL tests require an explicit opt-in.

Every session is UUID-scoped, every business SQLite database is tmp_path-scoped.
No test touches the application's real orders.db.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import ValidationError

from checkpoint.models import (
    AgentCheckpoint, CheckpointConflict, CheckpointCorrupt,
    CheckpointOwnershipError, CheckpointUnavailable,
)
from checkpoint.store import CheckpointStore
from mcp.execution_recovery import ExecutionReconciler
from refunds.service import RefundService
from tickets.service import TicketService
from tests.test_orchestrator import _orchestrator, _state


def test_strict_json_validation_and_required_password(monkeypatch):
    for changes in (
        {"context": {"object": object()}}, {"context": {"float": float("nan")}},
        {"version": True}, {"messages": [{"role": "system", "content": "unsafe"}]},
        {"messages": [{"role": "user", "content": "x", "additional_kwargs": {}}]},
        {"current_stage": "WAIT_CONFIRM", "status": "waiting"}, {"session_id": " bad "},
        {"status": "finished"}, {"unknown": "field"},
        {"current_stage": "UNKNOWN"},
    ):
        with pytest.raises((ValueError, ValidationError)):
            AgentCheckpoint.model_validate({"session_id": "s", "user_id": "u", **changes})
    monkeypatch.delenv("MYSQL_PASSWORD", raising=False)
    with pytest.raises(ValueError, match="MYSQL_PASSWORD"):
        CheckpointStore.from_env()
    from agents.orchestrator import _CheckpointRun
    run = _CheckpointRun(None, AgentCheckpoint(session_id="s", user_id="u"), {})
    run.save = AsyncMock(side_effect=AssertionError("invalid plan was persisted"))
    with pytest.raises(CheckpointCorrupt):
        asyncio.run(run.ticket_plan(AsyncMock(return_value={"summary": ["not text"]})))


@pytest.fixture
async def mysql():
    if os.getenv("SMARTCS_CHECKPOINT_MYSQL_TEST") != "1":
        pytest.skip("real MySQL disabled; set SMARTCS_CHECKPOINT_MYSQL_TEST=1 explicitly")
    from dotenv import load_dotenv
    load_dotenv()
    store = CheckpointStore.from_env()
    await store.initialize()
    sid = "cp-test-" + uuid.uuid4().hex
    yield store, sid
    async with store.session_lock(sid):
        cp = await store.load(sid, "user_002")
        if cp:
            await store.delete(sid, cp.user_id, cp.version)


def runtime(tmp_path, store, **kwargs):
    orchestrator, sessions, short, executor, repository = _orchestrator(tmp_path, **kwargs)
    orchestrator.checkpoint_store = store
    orchestrator.execution_reconciler = ExecutionReconciler(
        executor.ledger, RefundService(repository), TicketService(repository)
    )
    return orchestrator, sessions, short, executor, repository


def request(sid, text, rid):
    return {**_state(text, session_id=sid), "client_request_id": rid}


async def interrupt_at(store, stage, action):
    original = store.update
    async def update(cp):
        saved = await original(cp)
        if saved.current_stage == stage:
            raise RuntimeError("injected process boundary")
        return saved
    store.update = update
    try:
        with pytest.raises(RuntimeError, match="injected"):
            await action()
    finally:
        store.update = original


async def test_mysql_crud_ownership_cas_lock_and_corrupt_data(mysql):
    store, sid = mysql
    first = await store.save(AgentCheckpoint(session_id=sid, user_id="user_002"))
    assert first.version == 1
    with pytest.raises(CheckpointOwnershipError):
        await store.load(sid, "another-user")
    second = await store.update(first)
    assert second.version == 2
    with pytest.raises(CheckpointConflict):
        await store.update(first)
    with pytest.raises(CheckpointConflict):
        await store.save(first)
    other = CheckpointStore.from_env()
    async with store.session_lock(sid):
        with pytest.raises(CheckpointConflict):
            async with other.session_lock(sid):
                pytest.fail("two owners acquired one session")
    # Only this test's UUID row is altered. Restore it before fixture cleanup.
    await store._call(lambda _c, cursor: cursor.execute(
        "UPDATE agent_checkpoint SET state_json=%s WHERE session_id=%s", ('{"bad":true}', sid)))
    with pytest.raises(CheckpointCorrupt):
        await store.load(sid, "user_002")
    await store._call(lambda _c, cursor: cursor.execute(
        "UPDATE agent_checkpoint SET state_json=%s WHERE session_id=%s", (second.payload(), sid)))


@pytest.mark.parametrize("stage,remaining_calls", [("ROUTED", 1), ("EXECUTING", 1), ("GENERATED", 1), ("REVIEWED", 0)])
async def test_node_resume_skips_finished_nodes_and_turn_increment(mysql, tmp_path, stage, remaining_calls):
    store, sid = mysql
    agent, *_ = runtime(tmp_path, store)
    await interrupt_at(store, stage, lambda: agent.ainvoke(request(sid, "查询订单 ORD-20260801-0002", "r1")))
    before = await store.load(sid, "user_002")
    assert before.context["session_state"]["turn_count"] == 1
    restarted, *_ = runtime(tmp_path, CheckpointStore.from_env())
    result = await restarted.resume(sid, "user_002", "r1")
    assert "ORD-20260801-0002" in result["final_response"]
    assert restarted.llm.call_count == remaining_calls
    finished = await store.load(sid, "user_002")
    assert finished.context["session_state"]["turn_count"] == 1
    assert len(finished.messages) == 2


async def test_wait_confirm_cancel_stale_redis_and_new_turn(mysql, tmp_path):
    store, sid = mysql
    agent, *_ = runtime(tmp_path, store)
    await agent.ainvoke(request(sid, "帮我退款 ORD-20260801-0002", "prepare"))
    pending = (await store.load(sid, "user_002")).pending_action
    restarted, sessions, _, _, repo = runtime(tmp_path, store)
    await sessions.set_pending_action(sid, pending)  # deliberately stale volatile state
    await restarted.ainvoke(request(sid, "取消退款", "cancel"))
    cp = await store.load(sid, "user_002")
    assert cp.pending_action is None
    await restarted.ainvoke(request(sid, "确认退款", "late-confirm"))
    assert repo.get_order("ORD-20260801-0002")["refunds"] == []
    assert (await store.load(sid, "user_002")).context["session_state"]["turn_count"] == 3


async def test_request_receipts_survive_later_turns_and_conflict(mysql, tmp_path):
    store, sid = mysql
    agent, *_ = runtime(tmp_path, store)
    original = request(sid, "查询订单 ORD-20260801-0002", "first")
    result = await agent.ainvoke(original)
    await agent.ainvoke(request(sid, "查询订单 ORD-20260801-0003", "second"))
    agent.llm.ainvoke = AsyncMock(side_effect=AssertionError("replay called model"))
    assert (await agent.ainvoke(original))["final_response"] == result["final_response"]
    with pytest.raises(CheckpointConflict):
        await agent.ainvoke(request(sid, "different", "first"))
    assert len((await store.load(sid, "user_002")).messages) == 4


async def test_prewrite_checkpoint_failure_blocks_refund(mysql, tmp_path):
    store, sid = mysql
    agent, _, _, _, repository = runtime(tmp_path, store)
    await agent.ainvoke(request(sid, "帮我退款 ORD-20260801-0002", "prepare"))
    original = store.update
    async def fail(cp):
        if cp.context.get("effects"):
            raise CheckpointUnavailable("injected storage failure")
        return await original(cp)
    store.update = fail
    try:
        with pytest.raises(CheckpointUnavailable):
            await agent.ainvoke(request(sid, "确认退款", "confirm"))
        assert repository.get_order("ORD-20260801-0002")["refunds"] == []
    finally:
        store.update = original
    result = await agent.resume(sid, "user_002", "confirm")
    assert "已提交" in result["final_response"]
    assert len(repository.get_order("ORD-20260801-0002")["refunds"]) == 1


async def test_ticket_plan_and_completed_ledger_are_reused(mysql, tmp_path):
    store, sid = mysql
    overrides = {"intent_router": {"primary_intent": "complaint", "secondary_intent": "complaint",
        "suggested_agent": "ticket_handler", "confidence": 0.99, "entities": {}},
        "ticket_handler": {"action": "create", "ticket_type": "complaint", "priority": "medium",
                           "summary": "服务投诉", "details": "需要人工处理", "debug_trace": "must_not_save"}}
    agent, _, _, executor, _ = runtime(tmp_path, store, overrides=overrides)
    original = executor.ledger.complete
    def crash(key, result):
        original(key, result)
        raise RuntimeError("injected crash after ledger")
    executor.ledger.complete = crash
    with pytest.raises(RuntimeError, match="injected"):
        await agent.ainvoke(request(sid, "帮我创建投诉工单", "ticket"))
    assert "debug_trace" not in (await store.load(sid, "user_002")).context["ticket_plan"]
    restarted, _, _, tools, _ = runtime(tmp_path, store)
    restarted.ticket_agent.analyze_request = AsyncMock(side_effect=AssertionError("ticket plan regenerated"))
    tools.server.get_tool("ticket_create").handler = lambda **_: pytest.fail("business write repeated")
    result = await restarted.resume(sid, "user_002", "ticket")
    assert "工单已创建成功" in result["final_response"]


async def test_compliance_escalation_crash_replays_without_second_ticket(mysql, tmp_path):
    store, sid = mysql
    overrides = {"compliance": {"passed": False, "risk_level": "high", "violations": ["敏感内容"]}}
    agent, _, _, executor, _ = runtime(tmp_path, store, overrides=overrides)
    original = executor.ledger.complete
    def crash(key, result):
        original(key, result)
        raise RuntimeError("injected escalation crash")
    executor.ledger.complete = crash
    with pytest.raises(RuntimeError, match="injected"):
        await agent.ainvoke(request(sid, "查询订单 ORD-20260801-0002", "escalate"))
    restarted, _, _, tools, _ = runtime(tmp_path, store)
    restarted.llm.ainvoke = AsyncMock(side_effect=AssertionError("reviewed stage called model"))
    tools.server.get_tool("ticket_create").handler = lambda **_: pytest.fail("escalation repeated")
    result = await restarted.resume(sid, "user_002")
    assert result["compliance_passed"] is False
    assert "转交人工" in result["final_response"]


async def test_concurrent_request_and_new_message_cannot_steal_active_run(mysql, tmp_path):
    store, sid = mysql
    agent, *_ = runtime(tmp_path, store)
    started, proceed = asyncio.Event(), asyncio.Event()
    handler = agent._handle
    async def pause(state):
        started.set()
        await proceed.wait()
        return await handler(state)
    agent._handle = pause
    task = asyncio.create_task(agent.ainvoke(request(sid, "查询订单 ORD-20260801-0002", "first")))
    await started.wait()
    try:
        other, *_ = runtime(tmp_path, CheckpointStore.from_env())
        with pytest.raises(CheckpointConflict):
            await other.resume(sid, "user_002")
        with pytest.raises(CheckpointConflict):
            await other.ainvoke(request(sid, "新的请求", "second"))
    finally:
        proceed.set()
        await task


async def test_http_ownership_history_delete_and_safe_errors(mysql, tmp_path, monkeypatch):
    from api import main as api
    from auth.context import UserContext
    from types import SimpleNamespace
    store, sid = mysql
    agent, *_ = runtime(tmp_path, store)
    monkeypatch.setattr(api, "checkpoint_store", store)
    monkeypatch.setattr(api, "chat_orchestrator", agent)
    monkeypatch.setattr(api, "session_store", agent.session_store)
    # Component test isolates session lookup and authentication only. The real
    # cookie/JWT + platform MySQL contract lives in test_user_sessions.py.
    owner = UserContext(account_id=1, username="owner", business_user_id="user_002")
    other = UserContext(account_id=2, username="other", business_user_id="wrong")
    current = [owner]
    monkeypatch.setitem(api.app.dependency_overrides, api.get_current_user, lambda: current[0])
    monkeypatch.setattr(api.app.state, "platform_sessions", SimpleNamespace(
        get_owned=AsyncMock(side_effect=lambda session_id, account_id:
                            {"session_id": sid} if session_id == sid and account_id == 1 else None),
        touch=AsyncMock(),
    ), raising=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url="http://test") as client:
        body = {"session_id": sid, "message": "查询订单 ORD-20260801-0002", "client_request_id": "http"}
        assert (await client.post("/api/chat", json=body)).status_code == 200
        current[0] = other
        assert (await client.get(f"/api/checkpoints/{sid}")).status_code == 404
        assert (await client.get(f"/api/history/{sid}")).status_code == 404
        assert (await client.delete(f"/api/history/{sid}")).status_code == 404
        current[0] = owner
        history = await client.get(f"/api/history/{sid}")
        assert len(history.json()["messages"]) == 2
        assert (await client.delete(f"/api/history/{sid}")).status_code == 200
        assert (await client.get(f"/api/checkpoints/{sid}")).status_code == 404


async def test_unsupported_version_is_fail_closed(mysql, tmp_path):
    store, sid = mysql
    agent, *_ = runtime(tmp_path, store)
    await interrupt_at(store, "ROUTED", lambda: agent.ainvoke(request(sid, "查询订单 ORD-20260801-0002", "first")))
    cp = await store.load(sid, "user_002")
    cp.context["workflow_version"] = 999
    await store.update(cp)
    with pytest.raises(CheckpointCorrupt):
        await agent.resume(sid, "user_002")
    with pytest.raises(CheckpointCorrupt):
        await agent.ainvoke(request(sid, "新请求", "new"))
    cp = await store.load(sid, "user_002")
    cp.context["workflow_version"] = 1
    del cp.context["state"]["compliance_passed"]
    await store.update(cp)
    with pytest.raises(CheckpointCorrupt):
        await agent.resume(sid, "user_002")
