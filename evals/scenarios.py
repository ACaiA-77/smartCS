"""Functional and failure-injection scenarios for the offline agent eval."""

from __future__ import annotations

import tempfile
import gc
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from langchain_core.messages import HumanMessage

from agents.orchestrator import ChatOrchestrator
from evals.doubles import DeterministicEvalLLM, OfflineShortTermMemory
from evals.faults import FaultInjectingHandler, FaultPlan
from evals.models import EvalCheck
from memory.long_term import HashEmbeddingBackend, LongTermMemory
from memory.session_store import SessionStore
from mcp.approval_store import ApprovalService
from mcp.execution_ledger import ExecutionLedger
from mcp.mcp_server import MCPToolServer, create_default_tools
from mcp.order_repository import OrderRepository
from mcp.tool_execution import ToolExecutionPolicy, ToolExecutor


ORDER_ID = "ORD-20260801-0002"
ORDER_USER = "user_002"


@dataclass
class EvalRuntime:
    tempdir: tempfile.TemporaryDirectory[str]
    repository: OrderRepository
    long_term_memory: LongTermMemory
    short_term_memory: OfflineShortTermMemory
    session_store: SessionStore
    server: MCPToolServer
    ledger: ExecutionLedger
    approvals: ApprovalService
    actual_executor: ToolExecutor
    executor: "ObservedExecutor"
    llm: DeterministicEvalLLM
    orchestrator: ChatOrchestrator
    injected: dict[str, FaultInjectingHandler]

    def close(self) -> None:
        gc.collect()
        self.tempdir.cleanup()


class ObservedExecutor:
    """Record real executor results without changing execution semantics."""

    def __init__(self, executor: ToolExecutor):
        self.actual = executor
        self.calls: list[dict[str, Any]] = []

    @property
    def ledger(self) -> ExecutionLedger:
        return self.actual.ledger

    async def execute(self, name: str, arguments: dict[str, Any], context=None):
        result = await self.actual.execute(name, arguments, context)
        self.calls.append(
            {"name": name, "arguments": dict(arguments), "context": context, "result": result}
        )
        return result

    def results_for(self, name: str) -> list[Any]:
        return [call["result"] for call in self.calls if call["name"] == name]


@dataclass(frozen=True)
class EvalScenario:
    case_id: str
    category: str
    run: Callable[[], Awaitable[list[EvalCheck]]]
    critical: bool = True


def build_runtime(
    *,
    overrides: dict[str, Any] | None = None,
    timeout_seconds: float = 0.05,
) -> EvalRuntime:
    tempdir = tempfile.TemporaryDirectory(prefix="smartcs-eval-")
    root = Path(tempdir.name)
    repository = OrderRepository(str(root / "orders.db"))
    long_term_memory = LongTermMemory(
        index_path=str(root / "vectors" / "index"),
        embedding_backend=HashEmbeddingBackend(dimension=64),
    )
    long_term_memory.add_document(
        "退款流程：确认订单和退款条件后提交申请，退款原路退回。",
        "refund_policy.md",
    )
    long_term_memory.add_document(
        "投诉流程：说明服务问题后可以申请人工客服处理。",
        "complaint_policy.md",
    )
    short_term_memory = OfflineShortTermMemory(max_turns=50)
    session_store = SessionStore(short_term_memory)
    server = create_default_tools(
        MCPToolServer(),
        long_term_memory=long_term_memory,
        order_repository=repository,
    )
    ledger = ExecutionLedger(repository.db_path)
    approvals = ApprovalService(repository.db_path)
    actual_executor = ToolExecutor(
        server,
        policy=ToolExecutionPolicy(timeout_seconds=timeout_seconds, max_read_attempts=2),
        ledger=ledger,
        approval_service=approvals,
    )
    executor = ObservedExecutor(actual_executor)
    llm = DeterministicEvalLLM(overrides)
    orchestrator = ChatOrchestrator(
        llm=llm,
        session_store=session_store,
        long_term_memory=long_term_memory,
        mcp_server=server,
        tool_executor=executor,
    )
    return EvalRuntime(
        tempdir,
        repository,
        long_term_memory,
        short_term_memory,
        session_store,
        server,
        ledger,
        approvals,
        actual_executor,
        executor,
        llm,
        orchestrator,
        {},
    )


def inject(runtime: EvalRuntime, tool_name: str, plan: FaultPlan) -> FaultInjectingHandler:
    tool = runtime.server.get_tool(tool_name)
    if tool is None:
        raise KeyError(tool_name)
    wrapper = FaultInjectingHandler(tool.handler, plan)
    tool.handler = wrapper
    runtime.injected[tool_name] = wrapper
    return wrapper


def _state(message: str, *, user_id: str = ORDER_USER, session_id: str = "eval-session") -> dict[str, Any]:
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


async def _ask(runtime: EvalRuntime, message: str, **kwargs: Any) -> dict[str, Any]:
    return await runtime.orchestrator.ainvoke(_state(message, **kwargs))


def _count(runtime: EvalRuntime, table: str, where: str = "", args: tuple[Any, ...] = ()) -> int:
    query = f"SELECT COUNT(*) FROM {table}"
    if where:
        query += f" WHERE {where}"
    with runtime.repository.transaction() as connection:
        return int(connection.execute(query, args).fetchone()[0])


def _refund_count(runtime: EvalRuntime, order_id: str = ORDER_ID) -> int:
    return _count(runtime, "refunds", "order_id = ?", (order_id,))


def _ticket_count(runtime: EvalRuntime, user_id: str | None = None) -> int:
    if user_id is None:
        return _count(runtime, "support_tickets")
    return _count(runtime, "support_tickets", "user_id = ?", (user_id,))


def _ticket_id(runtime: EvalRuntime, user_id: str) -> str:
    with runtime.repository.transaction() as connection:
        row = connection.execute(
            "SELECT ticket_id FROM support_tickets WHERE user_id = ? ORDER BY created_at, ticket_id LIMIT 1",
            (user_id,),
        ).fetchone()
    if row is None:
        return ""
    return str(row[0])


def _ledger_status(runtime: EvalRuntime, key: str) -> str | None:
    record = runtime.ledger.get(key)
    return record.get("status") if record else None


def _eq(name: str, expected: Any, actual: Any, metric: str | None = None) -> EvalCheck:
    return EvalCheck(name, actual == expected, expected, actual, metric)


def _true(name: str, actual: Any, metric: str | None = None) -> EvalCheck:
    return EvalCheck(name, bool(actual), True, bool(actual), metric)


def _ticket_content_disclosure_checks(
    title: str, description: str, tool_result: Any, response: str
) -> list[EvalCheck]:
    tool_text = str(tool_result)
    return [
        _true("tool_result_has_no_title", title not in tool_text),
        _true("tool_result_has_no_description", description not in tool_text),
        _true("agent_response_has_no_title", title not in response),
        _true("agent_response_has_no_description", description not in response),
    ]


async def case_01_order_query() -> list[EvalCheck]:
    runtime = build_runtime()
    try:
        before_refunds = _refund_count(runtime)
        before_tickets = _ticket_count(runtime)
        result = await _ask(runtime, f"查询订单 {ORDER_ID}")
        response = result["final_response"]
        return [
            _eq("intent_is_order_query", "order_query", result.get("intent"), "routing"),
            _true("order_handler_path_used", "ticket_handler" in result["sub_results"], "routing"),
            _true("response_contains_order", ORDER_ID in response),
            _eq("no_ticket_created", before_tickets, _ticket_count(runtime), "side_effect_safety"),
            _eq("no_refund_created", before_refunds, _refund_count(runtime), "side_effect_safety"),
        ]
    finally:
        runtime.close()


async def case_02_knowledge_route() -> list[EvalCheck]:
    runtime = build_runtime(overrides={"rag_answer": "退款流程需要先确认订单和退款条件。"})
    try:
        result = await _ask(runtime, "退款流程是什么？")
        return [
            _eq("intent_is_knowledge_rag", "knowledge_rag", result.get("intent"), "routing"),
            _true("final_response_exists", bool(result.get("final_response"))),
            _eq("no_refund_created", 0, _refund_count(runtime), "side_effect_safety"),
            _eq("no_ticket_created", 0, _ticket_count(runtime), "side_effect_safety"),
        ]
    finally:
        runtime.close()


async def case_03_complaint_consultation() -> list[EvalCheck]:
    runtime = build_runtime(
        overrides={
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
                "details": "咨询投诉流程",
            },
        }
    )
    try:
        result = await _ask(runtime, "投诉流程是什么？")
        response = result["final_response"]
        return [
            _eq("intent_is_ticket_handler", "ticket_handler", result.get("intent"), "routing"),
            _eq("ticket_create_not_called", 0, len(runtime.executor.results_for("ticket_create")), "side_effect_safety"),
            _eq("support_tickets_unchanged", 0, _ticket_count(runtime), "side_effect_safety"),
            _true("response_does_not_claim_creation", "工单已创建成功" not in response),
        ]
    finally:
        runtime.close()


async def case_04_explicit_complaint_exactly_once() -> list[EvalCheck]:
    runtime = build_runtime(
        overrides={
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
                "details": "用户投诉服务问题",
            },
        }
    )
    try:
        first = await _ask(runtime, "我要投诉服务问题", session_id="complaint-session")
        ticket_id = _ticket_id(runtime, ORDER_USER)
        second = await _ask(runtime, "我要投诉服务问题", session_id="complaint-session")
        results = runtime.executor.results_for("ticket_create")
        return [
            _eq("intent_routes_to_ticket", "ticket_handler", first.get("intent"), "routing"),
            _eq("one_durable_ticket", 1, _ticket_count(runtime, ORDER_USER), "side_effect_safety"),
            _true("first_response_has_ticket_id", bool(ticket_id) and ticket_id in first["final_response"]),
            _true("second_response_has_ticket_id", ticket_id in second["final_response"]),
            _true("second_write_is_ledger_replay", len(results) == 2 and results[-1].replayed),
            _eq("ledger_completed", "completed", _ledger_status(runtime, "ticket:complaint-session:complaint:" + _ticket_payload_hash(runtime, ticket_id)), "side_effect_safety"),
        ]
    finally:
        runtime.close()


def _ticket_payload_hash(runtime: EvalRuntime, ticket_id: str) -> str:
    with runtime.repository.transaction() as connection:
        row = connection.execute(
            "SELECT payload_hash FROM support_tickets WHERE ticket_id = ?", (ticket_id,)
        ).fetchone()
    return str(row[0]) if row else ""


async def case_05_refund_confirmation() -> list[EvalCheck]:
    runtime = build_runtime()
    try:
        first = await _ask(runtime, f"帮我退款 {ORDER_ID}", session_id="refund-session")
        before_confirm = _refund_count(runtime)
        pending = (await runtime.session_store.get_state("refund-session")).pending_action
        second = await _ask(runtime, "确认退款", session_id="refund-session")
        after_confirm = _refund_count(runtime)
        third = await _ask(runtime, "确认退款", session_id="refund-session")
        after_replay = _refund_count(runtime)
        state = await runtime.session_store.get_state("refund-session")
        refund_result = runtime.executor.results_for("refund_create")[-1]
        return [
            _eq("intent_routes_to_refund", "refund_handler", first.get("intent"), "routing"),
            _true("pending_action_exists", isinstance(pending, dict)),
            _eq("refunds_before_confirmation", 0, before_confirm, "side_effect_safety"),
            _eq("one_refund_after_confirmation", 1, after_confirm, "side_effect_safety"),
            _eq("repeat_confirmation_is_exactly_once", 1, after_replay, "side_effect_safety"),
            _eq("pending_action_cleared", None, state.pending_action),
            _true("confirmation_response_reports_success", "退款申请已提交" in second["final_response"]),
            _true("ledger_write_completed", refund_result.success and _ledger_status(runtime, "refund:refund-session:" + ORDER_ID) == "completed"),
            _true("repeat_confirmation_does_not_claim_new_write", "当前没有待确认" in third["final_response"]),
        ]
    finally:
        runtime.close()


async def case_06_refund_cancellation() -> list[EvalCheck]:
    runtime = build_runtime()
    try:
        await _ask(runtime, f"帮我退款 {ORDER_ID}", session_id="cancel-session")
        cancelled = await _ask(runtime, "取消退款", session_id="cancel-session")
        state = await runtime.session_store.get_state("cancel-session")
        return [
            _eq("refunds_after_cancellation", 0, _refund_count(runtime), "side_effect_safety"),
            _eq("pending_action_cleared", None, state.pending_action, "side_effect_safety"),
            _true("cancellation_response", "已取消" in cancelled["final_response"]),
            _eq("refund_create_not_called", 0, len(runtime.executor.results_for("refund_create")), "side_effect_safety"),
        ]
    finally:
        runtime.close()


async def case_07_wrong_user_refund() -> list[EvalCheck]:
    runtime = build_runtime()
    try:
        result = await _ask(runtime, f"帮我退款 {ORDER_ID}", user_id="user_001", session_id="wrong-user")
        state = await runtime.session_store.get_state("wrong-user")
        order = runtime.repository.get_order(ORDER_ID)
        response = result["final_response"]
        return [
            _eq("refunds_unchanged", 0, _refund_count(runtime), "side_effect_safety"),
            _eq("pending_action_is_none", None, state.pending_action, "side_effect_safety"),
            _true("safe_response_has_no_amount", str(order["pay_amount"]) not in response),
            _true("safe_response_has_no_sensitive_order_detail", "可退款金额" not in response and "退款方式" not in response),
        ]
    finally:
        runtime.close()


async def case_08_ticket_ownership() -> list[EvalCheck]:
    runtime = build_runtime()
    try:
        owner = "user_002"
        other = "user_003"
        secret_title = "owner-secret-title"
        secret_description = "owner-secret-description"
        runtime.llm.set_override(
            "ticket_handler",
            {
                "action": "create",
                "ticket_type": "complaint",
                "priority": "medium",
                "summary": secret_title,
                "details": secret_description,
            },
        )
        await _ask(runtime, "我要投诉服务问题", user_id=owner, session_id="owner-session")
        ticket_id = _ticket_id(runtime, owner)
        with runtime.repository.transaction() as connection:
            owner_row = connection.execute(
                "SELECT title, description FROM support_tickets WHERE ticket_id = ?",
                (ticket_id,),
            ).fetchone()
        runtime.llm.set_override(
            "intent_router",
            {
                "primary_intent": "consultation",
                "secondary_intent": "ticket_query",
                "confidence": 0.95,
                "entities": {},
                "suggested_agent": "ticket_handler",
            },
        )
        runtime.llm.set_override(
            "ticket_handler",
            {"action": "query", "ticket_type": "general", "priority": "low"},
        )
        result = await _ask(runtime, f"查询工单 {ticket_id}", user_id=other, session_id="other-session")
        response = result["final_response"]
        query_result = runtime.executor.results_for("ticket_query")[-1].result
        persisted = (owner_row["title"], owner_row["description"]) if owner_row else (None, None)
        return [
            _eq("owner_ticket_exists", 1, _ticket_count(runtime, owner), "side_effect_safety"),
            _eq("persisted_owner_title", secret_title, persisted[0]),
            _eq("persisted_owner_description", secret_description, persisted[1]),
            _true("other_user_gets_safe_not_found", "未找到工单号" in response),
        ] + _ticket_content_disclosure_checks(
            secret_title, secret_description, query_result, response
        )
    finally:
        runtime.close()


async def case_09_low_confidence() -> list[EvalCheck]:
    runtime = build_runtime(
        overrides={
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
    )
    try:
        result = await _ask(runtime, "嗯", session_id="low-confidence")
        return [
            _eq("needs_clarification", True, result.get("needs_clarification"), "routing"),
            _eq("no_ticket_write", 0, _ticket_count(runtime), "side_effect_safety"),
            _eq("no_refund_write", 0, _refund_count(runtime), "side_effect_safety"),
            _true("clarification_response_exists", bool(result.get("final_response"))),
        ]
    finally:
        runtime.close()


async def case_10_compliance_escalation() -> list[EvalCheck]:
    runtime = build_runtime(
        overrides={
            "compliance": {
                "passed": False,
                "risk_level": "high",
                "violations": ["deterministic eval compliance failure"],
                "suggestions": [],
            }
        }
    )
    try:
        first = await _ask(runtime, "普通咨询", session_id="compliance-session")
        second = await _ask(runtime, "普通咨询", session_id="compliance-session")
        escalation_count = _count(
            runtime,
            "support_tickets",
            "category = 'compliance_escalation'",
        )
        results = runtime.executor.results_for("ticket_create")
        return [
            _eq("one_escalation_ticket", 1, escalation_count, "side_effect_safety"),
            _true("first_response_reports_escalation", "转交人工客服" in first["final_response"]),
            _true("same_session_reuses_effect", len(results) == 2 and results[-1].replayed),
            _eq("escalation_ledger_completed", "completed", _ledger_status(runtime, "compliance-escalation:compliance-session"), "side_effect_safety"),
            _true("second_response_reports_escalation", "转交人工客服" in second["final_response"]),
        ]
    finally:
        runtime.close()


async def case_11_transient_read() -> list[EvalCheck]:
    runtime = build_runtime()
    try:
        fault = inject(runtime, "order_query", FaultPlan.raise_once("transient read"))
        result = await _ask(runtime, f"查询订单 {ORDER_ID}", session_id="transient-read")
        tool_result = runtime.executor.results_for("order_query")[-1]
        return [
            _eq("fault_handler_calls", 2, fault.call_count, "failure_containment"),
            _eq("fault_handler_faults", 1, fault.fault_count, "failure_containment"),
            _eq("tool_executor_attempts", 2, tool_result.attempts, "failure_containment"),
            _true("final_request_succeeds", tool_result.success and ORDER_ID in result["final_response"], "failure_containment"),
            _eq("no_ticket_side_effect", 0, _ticket_count(runtime), "side_effect_safety"),
            _eq("no_refund_side_effect", 0, _refund_count(runtime), "side_effect_safety"),
        ]
    finally:
        runtime.close()


async def case_12_read_timeout() -> list[EvalCheck]:
    runtime = build_runtime(timeout_seconds=0.005)
    try:
        fault = inject(runtime, "order_query", FaultPlan.delay(0.05))
        result = await _ask(runtime, f"查询订单 {ORDER_ID}", session_id="read-timeout")
        tool_result = runtime.executor.results_for("order_query")[-1]
        return [
            _eq("fault_handler_calls", 2, fault.call_count, "failure_containment"),
            _eq("fault_handler_faults", 2, fault.fault_count, "failure_containment"),
            _eq("tool_executor_attempts", 2, tool_result.attempts, "failure_containment"),
            _eq("terminal_error_code", "timeout", tool_result.error_code, "failure_containment"),
            _true("response_reports_read_failure", "查询失败" in result["final_response"], "failure_containment"),
            _eq("no_ticket_side_effect", 0, _ticket_count(runtime), "side_effect_safety"),
            _eq("no_refund_side_effect", 0, _refund_count(runtime), "side_effect_safety"),
        ]
    finally:
        runtime.close()


async def case_13_ticket_write_error() -> list[EvalCheck]:
    runtime = build_runtime(
        overrides={
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
                "details": "用户投诉服务问题",
            },
        }
    )
    try:
        fault = inject(runtime, "ticket_create", FaultPlan.raise_always("ticket storage unavailable"))
        result = await _ask(runtime, "我要投诉服务问题", session_id="ticket-write-error")
        tool_result = runtime.executor.results_for("ticket_create")[-1]
        return [
            _eq("fault_handler_calls", 1, fault.call_count, "failure_containment"),
            _eq("tool_executor_attempts", 1, tool_result.attempts, "failure_containment"),
            _eq("support_tickets_unchanged", 0, _ticket_count(runtime), "side_effect_safety"),
            _eq("ledger_failed", "failed", _ledger_status(runtime, _write_key(runtime, "ticket-write-error")), "failure_containment"),
            _true("response_reports_failure", "工单已创建成功" not in result["final_response"] and "工单创建失败" in result["final_response"], "failure_containment"),
        ]
    finally:
        runtime.close()


def _write_key(runtime: EvalRuntime, session_id: str) -> str:
    for call in runtime.executor.calls:
        if call["name"] == "ticket_create":
            return str(call["context"].idempotency_key)
    return f"ticket:{session_id}"


async def case_14_refund_write_error() -> list[EvalCheck]:
    runtime = build_runtime()
    try:
        await _ask(runtime, f"帮我退款 {ORDER_ID}", session_id="refund-write-error")
        fault = inject(runtime, "refund_create", FaultPlan.raise_always("refund storage unavailable"))
        before = _refund_count(runtime)
        result = await _ask(runtime, "确认退款", session_id="refund-write-error")
        tool_result = runtime.executor.results_for("refund_create")[-1]
        key = "refund:refund-write-error:" + ORDER_ID
        return [
            _eq("fault_handler_calls", 1, fault.call_count, "failure_containment"),
            _eq("tool_executor_attempts", 1, tool_result.attempts, "failure_containment"),
            _eq("refund_rows_unchanged", before, _refund_count(runtime), "side_effect_safety"),
            _eq("ledger_failed", "failed", _ledger_status(runtime, key), "failure_containment"),
            _true("response_reports_failure", "退款提交暂时失败" in result["final_response"], "failure_containment"),
        ]
    finally:
        runtime.close()


FUNCTIONAL_SCENARIOS = [
    EvalScenario("functional_order_query", "functional", case_01_order_query),
    EvalScenario("functional_knowledge_route", "functional", case_02_knowledge_route),
    EvalScenario("functional_complaint_consultation", "functional", case_03_complaint_consultation),
    EvalScenario("functional_explicit_complaint", "functional", case_04_explicit_complaint_exactly_once),
    EvalScenario("functional_refund_confirmation", "functional", case_05_refund_confirmation),
    EvalScenario("functional_refund_cancellation", "functional", case_06_refund_cancellation),
    EvalScenario("functional_wrong_user_refund", "functional", case_07_wrong_user_refund),
    EvalScenario("functional_ticket_ownership", "functional", case_08_ticket_ownership),
    EvalScenario("functional_low_confidence", "functional", case_09_low_confidence),
    EvalScenario("functional_compliance_escalation", "functional", case_10_compliance_escalation),
]

FAULT_SCENARIOS = [
    EvalScenario("fault_transient_read", "fault", case_11_transient_read),
    EvalScenario("fault_read_timeout", "fault", case_12_read_timeout),
    EvalScenario("fault_ticket_write_error", "fault", case_13_ticket_write_error),
    EvalScenario("fault_refund_write_error", "fault", case_14_refund_write_error),
]

ALL_SCENARIOS = FUNCTIONAL_SCENARIOS + FAULT_SCENARIOS

if len({scenario.case_id for scenario in ALL_SCENARIOS}) != len(ALL_SCENARIOS):
    raise RuntimeError("eval case ids must be unique")


async def run_scenario(scenario: EvalScenario) -> list[EvalCheck]:
    return await scenario.run()
