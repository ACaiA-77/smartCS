"""Agent business paths use the shared ToolExecutor boundary."""

from __future__ import annotations

from pathlib import Path

import pytest

from agents.orchestrator import ChatOrchestrator
from agents.ticket_handler import TicketHandlerAgent
from langchain_core.messages import HumanMessage
from memory.long_term import LongTermMemory
from memory.session_store import SessionStore
from memory.short_term import ShortTermMemory
from mcp.execution_ledger import ExecutionLedger
from mcp.mcp_server import MCPToolServer, ToolDefinition, create_default_tools
from mcp.order_repository import OrderRepository
from mcp.tool_execution import ToolExecutionContext, ToolExecutionResult, ToolExecutor
from tests.conftest import MockLLM
from tickets.service import TicketService, canonical_ticket_payload_hash


def _server(name: str, handler, *, operation_type: str = "read") -> MCPToolServer:
    server = MCPToolServer()
    server.register_tool(
        ToolDefinition(
            name=name,
            description="boundary test",
            input_schema={"type": "object"},
            handler=handler,
            operation_type=operation_type,
            risk_level="medium" if operation_type == "write" else "low",
            requires_confirmation=operation_type == "write",
            retryable=False,
        )
    )
    return server


class RecordingExecutor:
    def __init__(self, executor):
        self.executor = executor
        self.calls: list[tuple[str, dict, ToolExecutionContext]] = []

    async def execute(self, name, arguments, context=None):
        context = context or ToolExecutionContext()
        self.calls.append((name, arguments, context))
        return await self.executor.execute(name, arguments, context)


def _process_state(message: str) -> dict:
    return {
        "messages": [HumanMessage(content=message)],
        "user_id": "user-1",
        "session_id": "session-1",
        "sub_results": {
            "intent_router": {
                "secondary": "complaint",
                "entities": {},
            }
        },
    }


@pytest.mark.parametrize(
    "message",
    [
        "我要投诉流程是什么？",
        "我要投诉的话怎么操作？",
        "怎么帮我创建投诉工单？",
        "我要申请理赔需要什么条件？",
        "开户需要什么材料？",
    ],
)
def test_mixed_consultation_never_counts_as_create_consent(message):
    assert TicketHandlerAgent._has_explicit_create_consent(message) is False


@pytest.mark.parametrize(
    "message",
    [
        "我要投诉服务问题",
        "请帮我创建投诉工单",
        "帮我提交投诉",
        "我要申请理赔",
        "帮我办理开户",
    ],
)
def test_explicit_business_request_counts_as_create_consent(message):
    assert TicketHandlerAgent._has_explicit_create_consent(message) is True


@pytest.mark.asyncio
async def test_order_query_uses_read_executor_without_idempotency():
    async def order_query(order_id: str, user_id: str) -> dict:
        return {"found": True, "order_id": order_id, "status": "pending"}

    recorder = RecordingExecutor(ToolExecutor(_server("order_query", order_query)))
    agent = TicketHandlerAgent(MockLLM(), tool_executor=recorder)

    response = await agent.query_order("ORD-1", "user-1")

    assert "订单查询结果" in response
    name, args, context = recorder.calls[0]
    assert name == "order_query"
    assert args == {"order_id": "ORD-1", "user_id": "user-1"}
    assert context.confirmed is False
    assert context.idempotency_key is None


@pytest.mark.asyncio
async def test_ticket_create_is_confirmed_keyed_and_replay_does_not_duplicate(tmp_path):
    calls = 0

    async def ticket_create(**kwargs) -> dict:
        nonlocal calls
        calls += 1
        return {"ticket_id": "TK-REMOTE-1", "status": "created"}

    ledger = ExecutionLedger(tmp_path / "ledger.db")
    recorder = RecordingExecutor(
        ToolExecutor(
            _server("ticket_create", ticket_create, operation_type="write"),
            ledger=ledger,
        )
    )
    agent = TicketHandlerAgent(MockLLM(), tool_executor=recorder)
    info = {
        "ticket_type": "complaint",
        "priority": "high",
        "summary": "服务问题",
        "details": "需要人工处理",
    }

    first = await agent.create_ticket(info, "user-1", "session-1")
    second = await agent.create_ticket(info, "user-1", "session-1")

    assert "TK-REMOTE-1" in first and second == first
    assert calls == 1
    assert recorder.calls[0][2].confirmed is True
    assert recorder.calls[0][2].idempotency_key.startswith("ticket:session-1:complaint:")
    assert recorder.calls[0][1]["client_request_id"].startswith("ticket-client:session-1:complaint:")
    assert recorder.calls[0][1]["user_id"] == "user-1"
    assert recorder.calls[0][1]["request_payload_hash"] == canonical_ticket_payload_hash(
        recorder.calls[0][1]
    )


@pytest.mark.asyncio
async def test_ticket_create_failure_does_not_claim_success_or_create_local_ticket(tmp_path):
    async def fail(**kwargs):
        return {"success": False, "reason_code": "backend_failure"}

    ledger = ExecutionLedger(tmp_path / "ledger.db")
    agent = TicketHandlerAgent(
        MockLLM(),
        tool_executor=ToolExecutor(
            _server("ticket_create", fail, operation_type="write"), ledger=ledger
        ),
    )

    response = await agent.create_ticket({"summary": "x", "details": "y"}, "u", "s")

    assert "创建失败" in response
    assert ledger.get("ticket:s:general:") is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message",
    ["投诉怎么操作", "投诉流程是什么", "理赔流程是什么", "我要投诉流程是什么"],
)
async def test_consultation_does_not_create_ticket_even_when_llm_says_create(message, tmp_path):
    calls = 0

    async def ticket_create(**kwargs) -> dict:
        nonlocal calls
        calls += 1
        return {"ticket_id": "TK-SHOULD-NOT-EXIST"}

    recorder = RecordingExecutor(
        ToolExecutor(
            _server("ticket_create", ticket_create, operation_type="write"),
            ledger=ExecutionLedger(tmp_path / "ledger.db"),
        )
    )
    agent = TicketHandlerAgent(
        MockLLM(
            overrides={
                "ticket_handler": {
                    "action": "create",
                    "ticket_type": "complaint",
                    "priority": "medium",
                    "summary": "投诉咨询",
                    "details": message,
                }
            }
        ),
        tool_executor=recorder,
    )

    result = await agent.process(_process_state(message))

    assert calls == 0
    assert recorder.calls == []
    assert "工单已创建成功" not in result["sub_results"]["ticket_handler"]
    assert "明确告诉我" in result["sub_results"]["ticket_handler"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message",
    ["我要投诉服务问题", "请帮我创建投诉工单", "帮我提交投诉", "我要申请理赔", "帮我办理开户"],
)
async def test_explicit_business_request_creates_ticket_with_confirmed_replay(message, tmp_path):
    calls = 0

    async def ticket_create(**kwargs) -> dict:
        nonlocal calls
        calls += 1
        return {"ticket_id": "TK-EXPLICIT-1"}

    recorder = RecordingExecutor(
        ToolExecutor(
            _server("ticket_create", ticket_create, operation_type="write"),
            ledger=ExecutionLedger(tmp_path / "ledger.db"),
        )
    )
    agent = TicketHandlerAgent(
        MockLLM(
            overrides={
                "ticket_handler": {
                    "action": "create",
                    "ticket_type": "complaint",
                    "priority": "medium",
                    "summary": "办理申请",
                    "details": message,
                }
            }
        ),
        tool_executor=recorder,
    )

    first = await agent.process(_process_state(message))
    second = await agent.process(_process_state(message))

    assert "工单已创建成功" in first["sub_results"]["ticket_handler"]
    assert second["sub_results"]["ticket_handler"] == first["sub_results"]["ticket_handler"]
    assert calls == 1
    assert recorder.calls[0][2].confirmed is True
    assert recorder.calls[0][2].idempotency_key


@pytest.mark.asyncio
async def test_update_action_does_not_fall_through_to_create(tmp_path):
    async def ticket_create(**kwargs) -> dict:
        raise AssertionError("update must not create a ticket")

    recorder = RecordingExecutor(
        ToolExecutor(
            _server("ticket_create", ticket_create, operation_type="write"),
            ledger=ExecutionLedger(tmp_path / "ledger.db"),
        )
    )
    agent = TicketHandlerAgent(
        MockLLM(
            overrides={
                "ticket_handler": {
                    "action": "update",
                    "ticket_type": "complaint",
                    "summary": "更新",
                    "details": "更新工单",
                }
            }
        ),
        tool_executor=recorder,
    )

    result = await agent.process(_process_state("更新我的工单"))

    assert "暂不支持更新工单" in result["sub_results"]["ticket_handler"]
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_compliance_escalation_replays_one_durable_ticket(tmp_path):
    repository = OrderRepository(str(tmp_path / "orders.db"))
    service = TicketService(repository)
    server = create_default_tools(
        MCPToolServer(), order_repository=repository, ticket_service=service
    )
    executor = ToolExecutor(server, ledger=ExecutionLedger(repository.db_path))
    orchestrator = ChatOrchestrator(
        MockLLM(),
        SessionStore(ShortTermMemory(redis_url="redis://127.0.0.1:6399/0")),
        LongTermMemory(),
        tool_executor=executor,
    )
    state = {"session_id": "session-1", "user_id": "user-1"}

    first = await orchestrator._create_escalation_ticket(state)
    second = await orchestrator._create_escalation_ticket(state)

    assert first == second
    with repository.transaction() as connection:
        assert connection.execute("SELECT COUNT(*) FROM support_tickets").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_missing_executor_gracefully_blocks_read_and_write():
    agent = TicketHandlerAgent(MockLLM())

    assert "服务暂不可用" in await agent.query_order("ORD-1", "u")
    assert "服务暂不可用" in await agent.create_ticket({"summary": "x"}, "u", "s")

    orchestrator = ChatOrchestrator(
        MockLLM(),
        SessionStore(ShortTermMemory(redis_url="redis://127.0.0.1:6399/0")),
        LongTermMemory(),
    )
    assert await orchestrator._create_escalation_ticket({"session_id": "s"}) == ""


def test_agents_have_no_direct_mcp_call_path():
    assert all(".call_tool(" not in path.read_text(encoding="utf-8") for path in Path("agents").glob("*.py"))
