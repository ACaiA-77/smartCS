from __future__ import annotations

from langchain_core.messages import HumanMessage

from agents.refund_handler import RefundHandlerAgent
from agents.orchestrator import create_chat_orchestrator
from mcp.execution_ledger import ExecutionLedger
from mcp.mcp_server import MCPToolServer, create_default_tools
from mcp.order_repository import OrderRepository
from mcp.tool_execution import ToolExecutor
from memory.long_term import LongTermMemory
from memory.session_store import SessionStore
from memory.short_term import ShortTermMemory
from tests.conftest import MockLLM


def _setup(tmp_path):
    repository = OrderRepository(str(tmp_path / "orders.db"))
    server = create_default_tools(MCPToolServer(), order_repository=repository)
    executor = ToolExecutor(server, ledger=ExecutionLedger(repository.db_path))
    return repository, executor


def _store() -> SessionStore:
    return SessionStore(ShortTermMemory(redis_url="redis://127.0.0.1:6399/0"))


def _state(message: str, session_id: str = "refund-session", user_id: str = "user_002"):
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


def _handler_state(message: str, secondary: str, user_id: str = "user_002"):
    state = _state(message, user_id=user_id)
    state["sub_results"] = {"intent_router": {"secondary": secondary, "entities": {}}}
    return state


async def _run(orchestrator, message: str):
    return await orchestrator.ainvoke(_state(message))


async def _pending(store: SessionStore, session_id: str = "refund-session"):
    return (await store.get_state(session_id)).pending_action


def test_refund_orchestrator_requires_confirmation_and_creates_once(tmp_path):
    import asyncio

    async def scenario():
        repository, executor = _setup(tmp_path)
        session_store = _store()
        orchestrator = create_chat_orchestrator(
            llm=MockLLM(),
            session_store=session_store,
            long_term_memory=LongTermMemory(),
            mcp_server=executor.server,
            tool_executor=executor,
        )

        first = await _run(orchestrator, "帮我退款 ORD-20260801-0002")
        assert repository.get_order("ORD-20260801-0002")["refunds"] == []
        pending = await _pending(session_store)
        assert pending and pending["type"] == "refund_create"
        assert "可退款金额" in first["final_response"]
        assert "退款方式：仅退款" in first["final_response"]

        second = await _run(orchestrator, "确认退款")
        order = repository.get_order("ORD-20260801-0002")
        assert len(order["refunds"]) == 1
        assert order["status"] == "refund_pending"
        assert "退款单号" in second["final_response"]
        await _run(orchestrator, "确认退款")
        assert len(repository.get_order("ORD-20260801-0002")["refunds"]) == 1

    asyncio.run(scenario())


def test_refund_mode_return_and_refund_is_localized(tmp_path):
    import asyncio

    async def scenario():
        _, executor = _setup(tmp_path)
        session_store = _store()
        orchestrator = create_chat_orchestrator(
            llm=MockLLM(), session_store=session_store,
            long_term_memory=LongTermMemory(), mcp_server=executor.server,
            tool_executor=executor,
        )
        result = await orchestrator.ainvoke(_state("帮我退款 ORD-20260801-0004", user_id="user_004"))
        assert "退款方式：退货退款" in result["final_response"]
        assert RefundHandlerAgent._format_refund_mode("unexpected_mode") == "unexpected_mode"

    asyncio.run(scenario())


def test_business_failure_clears_pending_without_writing_refund(tmp_path):
    import asyncio

    async def scenario():
        repository, executor = _setup(tmp_path)
        session_store = _store()
        orchestrator = create_chat_orchestrator(
            llm=MockLLM(), session_store=session_store,
            long_term_memory=LongTermMemory(), mcp_server=executor.server,
            tool_executor=executor,
        )
        await _run(orchestrator, "帮我退款 ORD-20260801-0002")
        with repository.transaction() as connection:
            connection.execute(
                "UPDATE orders SET status = 'cancelled', status_label = '已取消' WHERE order_id = ?",
                ("ORD-20260801-0002",),
            )
        result = await _run(orchestrator, "确认退款")
        assert "退款申请未提交" in result["final_response"]
        assert await _pending(session_store) is None
        assert repository.get_order("ORD-20260801-0002")["refunds"] == []

    asyncio.run(scenario())


def test_refund_cancel_has_no_write(tmp_path):
    import asyncio

    async def scenario():
        repository, executor = _setup(tmp_path)
        session_store = _store()
        orchestrator = create_chat_orchestrator(
            llm=MockLLM(), session_store=session_store,
            long_term_memory=LongTermMemory(), mcp_server=executor.server,
            tool_executor=executor,
        )
        await _run(orchestrator, "帮我退款 ORD-20260801-0002")
        result = await _run(orchestrator, "取消退款")
        assert "取消" in result["final_response"]
        assert await _pending(session_store) is None
        assert repository.get_order("ORD-20260801-0002")["refunds"] == []
        restarted = SessionStore(session_store.short_term_memory)
        assert await _pending(restarted) is None

    asyncio.run(scenario())


def test_new_request_invalidates_old_pending_before_order_lookup(tmp_path):
    import asyncio

    async def scenario():
        repository, executor = _setup(tmp_path)
        session_store = _store()
        orchestrator = create_chat_orchestrator(
            llm=MockLLM(), session_store=session_store,
            long_term_memory=LongTermMemory(), mcp_server=executor.server,
            tool_executor=executor,
        )
        await _run(orchestrator, "帮我退款 ORD-20260801-0002")
        handler = RefundHandlerAgent(tool_executor=executor, session_store=session_store)
        changed = await handler.process(_handler_state("帮我换一个订单退款", "refund_request"))
        assert "请提供订单号" in changed["sub_results"]["refund_handler"]
        assert await _pending(session_store) is None
        confirmed = await handler.process(_handler_state("确认", "refund_confirm"))
        assert "没有待确认" in confirmed["sub_results"]["refund_handler"]
        assert repository.get_order("ORD-20260801-0002")["refunds"] == []

    asyncio.run(scenario())


def test_ineligible_new_order_invalidates_old_pending(tmp_path):
    import asyncio

    async def scenario():
        repository, executor = _setup(tmp_path)
        session_store = _store()
        orchestrator = create_chat_orchestrator(
            llm=MockLLM(), session_store=session_store,
            long_term_memory=LongTermMemory(), mcp_server=executor.server,
            tool_executor=executor,
        )
        await _run(orchestrator, "帮我退款 ORD-20260801-0002")
        handler = RefundHandlerAgent(tool_executor=executor, session_store=session_store)
        rejected = await handler.process(
            _handler_state("帮我退款 ORD-20260801-0001", "refund_request", user_id="user_001")
        )
        assert "尚未完成支付" in rejected["sub_results"]["refund_handler"]
        assert await _pending(session_store) is None
        assert repository.get_order("ORD-20260801-0002")["refunds"] == []

    asyncio.run(scenario())


def test_confirm_user_mismatch_clears_pending_without_execution(tmp_path):
    import asyncio

    async def scenario():
        repository, executor = _setup(tmp_path)
        session_store = _store()
        orchestrator = create_chat_orchestrator(
            llm=MockLLM(), session_store=session_store,
            long_term_memory=LongTermMemory(), mcp_server=executor.server,
            tool_executor=executor,
        )
        await _run(orchestrator, "帮我退款 ORD-20260801-0002")
        handler = RefundHandlerAgent(tool_executor=executor, session_store=session_store)
        result = await handler.process(_handler_state("确认退款", "refund_confirm", user_id="user_001"))
        response = result["sub_results"]["refund_handler"]
        assert "未执行退款" in response
        assert "ORD-" not in response and "金额" not in response
        assert await _pending(session_store) is None
        assert repository.get_order("ORD-20260801-0002")["refunds"] == []

    asyncio.run(scenario())


def test_confirm_replays_same_pending_action_after_success(tmp_path):
    import asyncio

    async def scenario():
        repository, executor = _setup(tmp_path)
        session_store = _store()
        orchestrator = create_chat_orchestrator(
            llm=MockLLM(), session_store=session_store,
            long_term_memory=LongTermMemory(), mcp_server=executor.server,
            tool_executor=executor,
        )
        await _run(orchestrator, "帮我退款 ORD-20260801-0002")
        pending = await _pending(session_store)
        assert pending
        await _run(orchestrator, "确认退款")
        assert len(repository.get_order("ORD-20260801-0002")["refunds"]) == 1
        await session_store.set_pending_action("refund-session", pending)
        replay = await _run(orchestrator, "确认退款")
        assert "退款申请已提交" in replay["final_response"]
        assert executor.ledger.get(pending["idempotency_key"])["status"] == "completed"
        assert len(repository.get_order("ORD-20260801-0002")["refunds"]) == 1

    asyncio.run(scenario())


def test_wrong_user_does_not_persist_pending(tmp_path):
    import asyncio

    async def scenario():
        repository, executor = _setup(tmp_path)
        session_store = _store()
        orchestrator = create_chat_orchestrator(
            llm=MockLLM(), session_store=session_store,
            long_term_memory=LongTermMemory(), mcp_server=executor.server,
            tool_executor=executor,
        )
        state = _state("帮我退款 ORD-20260801-0002")
        state["user_id"] = "user_001"
        result = await orchestrator.ainvoke(state)
        assert "无法办理" in result["final_response"]
        assert await _pending(session_store) is None
        assert repository.get_order("ORD-20260801-0002")["refunds"] == []

    asyncio.run(scenario())


def test_pending_action_state_survives_new_store_instance():
    import asyncio

    async def scenario():
        short = ShortTermMemory(redis_url="redis://127.0.0.1:6399/0")
        first = SessionStore(short)
        restarted = SessionStore(short)
        action = {
            "type": "refund_create",
            "order_id": "ORD-20260801-0002",
            "user_id": "user_002",
            "amount": 128.0,
            "refund_mode": "refund_only",
            "reason": "用户申请退款",
            "idempotency_key": "refund:session:ORD-20260801-0002",
            "arguments": {
                "order_id": "ORD-20260801-0002",
                "user_id": "user_002",
                "reason": "用户申请退款",
            },
        }
        await first.set_pending_action("session", action)
        restored = (await restarted.get_state("session")).pending_action
        assert restored["idempotency_key"] == action["idempotency_key"]

    asyncio.run(scenario())
