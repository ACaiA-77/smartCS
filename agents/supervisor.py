"""
Supervisor编排Agent — 中央协调者
负责接收用户请求，根据意图路由到对应子Agent，汇总结果返回。
采用LangGraph StateGraph实现串行编排；MemorySaver + thread_id 已配置 Checkpoint。
"""

from __future__ import annotations

import os
from typing import Annotated, Any, TypedDict

from langchain_core.messages import AIMessage, BaseMessage
from langchain_openai import ChatOpenAI
from tracing.otel_config import create_traced_chat_openai
from langgraph.graph import END, StateGraph
from langgraph.graph.message import add_messages
from langgraph.checkpoint.memory import MemorySaver

from agents.intent_router import IntentRouterAgent
from agents.knowledge_rag import KnowledgeRAGAgent
from agents.ticket_handler import TicketHandlerAgent
from agents.compliance_checker import ComplianceCheckerAgent
from memory.working_memory import WorkingMemory
from memory.short_term import ShortTermMemory
from memory.long_term import LongTermMemory
from mcp.mcp_server import MCPToolServer
from tracing.otel_config import trace_agent_call


# ─── 状态定义 ───

class AgentState(TypedDict):
    """Supervisor编排的全局状态"""
    messages: Annotated[list[BaseMessage], add_messages]
    user_id: str
    session_id: str
    intent: str
    sub_results: dict[str, Any]
    compliance_passed: bool
    final_response: str
    current_agent: str
    retry_count: int
    needs_clarification: bool
    response_mode: str



_POLICY_OR_APPLICATION_SECONDARIES = {
    "return_refund_policy",
    "subscription_policy",
    "refund_request",
    "subscription_cancel_request",
}
_ORDER_OR_REPAIR_SECONDARIES = {
    "order_query",
    "repair_warranty_policy",
    "repair_request",
}
_ACCOUNT_SECONDARIES = {"account_guidance", "account_security", "fraud_report", "sensitive_data"}
_SECURITY_GUIDANCE = {
    "account_security": (
        "为保护您的 Apple 账户安全，请不要提供验证码、密码或受信任设备验证码。"
        "请尽快访问 iforgot.apple.com 重设密码，并检查账户中的受信任设备和登录记录。"
    ),
    "fraud_report": (
        "请立即停止与可疑人员互动，不要付款、不要提供验证码或账户信息，并保留聊天记录和付款凭证。"
        "请通过 Apple 官方支持渠道核实或报告该情况。"
    ),
    "sensitive_data": (
        "请不要继续发送身份证件、银行卡、密码或验证码等敏感信息。"
        "如已发送，请删除可撤回的信息，并通过 Apple 官方支持渠道获取后续帮助。"
    ),
    "prohibited_request": (
        "我无法协助获取、破解或绕过他人的 Apple 账户和设备安全措施。"
        "如您需要找回自己的账户，请使用 Apple 官方账户恢复流程。"
    ),
}


def build_clarification_message(intent_info: dict[str, Any]) -> str:
    """Return deterministic Apple-support clarification without legacy financial wording."""
    secondary_values = {str(intent_info.get("secondary", ""))}
    secondary_values.update(
        str(candidate.get("secondary_intent", ""))
        for candidate in intent_info.get("candidates", [])
        if isinstance(candidate, dict)
    )
    if secondary_values & _POLICY_OR_APPLICATION_SECONDARIES:
        return "您想了解 Apple 售后政策，还是希望我协助发起退款、退货或取消订阅申请？请说明具体产品和需求。"
    if secondary_values & _ORDER_OR_REPAIR_SECONDARIES:
        return "请告诉我是要查询 Apple 订单，还是需要维修设备；如有订单号或工单号，也可以一并提供。"
    if secondary_values & _ACCOUNT_SECONDARIES:
        return "请说明这是 Apple 账户使用问题，还是账户安全异常；请不要发送密码、验证码或其他敏感信息。"
    return "我还不确定您的具体 Apple 售后需求。请补充说明是产品使用或维修、订单/退款，还是 Apple 账户相关情况。"


def build_security_guidance(secondary_intent: str) -> str:
    """Return safe, non-transactional guidance for security-related intents."""
    return _SECURITY_GUIDANCE.get(
        secondary_intent,
        "为保护您的 Apple 账户和隐私，请不要提供密码、验证码或敏感信息，并通过 Apple 官方支持渠道获取帮助。",
    )

# ─── Supervisor节点 ───

class SupervisorNode:
    """Supervisor决策节点"""

    def __init__(
        self,
        llm: ChatOpenAI,
        working_memory: WorkingMemory,
        short_term_memory: ShortTermMemory | None = None,
        mcp_server: MCPToolServer | None = None,
    ):
        self.llm = llm
        self.working_memory = working_memory
        self.short_term_memory = short_term_memory
        self.mcp_server = mcp_server

    @trace_agent_call("supervisor")
    async def route_decision(self, state: AgentState) -> AgentState:
        """Supervisor 入口：读取工作记忆，注入 sub_results 供下游消费"""
        session_id = state.get("session_id", "default")
        ctx = self.working_memory.get_context(session_id)

        if self.short_term_memory is not None:
            dialog = await self.short_term_memory.get_context_window(session_id, max_tokens=2000)
            if dialog:
                ctx["dialog_context"] = dialog
                self.working_memory.update(session_id, ctx)

        return {
            **state,
            "current_agent": "supervisor",
            "needs_clarification": False,
            "sub_results": {
                **state.get("sub_results", {}),
                "_wm_context": {
                    "last_intent": ctx.get("last_intent"),
                    "accumulated_entities": ctx.get("accumulated_entities", {}),
                    "turn_count": ctx.get("turn_count", 0),
                },
            },
        }

    @staticmethod
    def present_clarification(state: AgentState) -> AgentState:
        """Mark a deterministic business clarification without invoking compliance review."""
        return {
            **state,
            "current_agent": "clarification",
            "response_mode": "clarification",
        }

    async def _create_escalation_ticket(self, state: AgentState) -> str:
        """合规失败时通过 MCP 创建转人工工单"""
        if self.mcp_server is None:
            return ""
        session_id = state.get("session_id", "unknown")
        result = await self.mcp_server.call_tool(
            "ticket_create",
            {
                "title": "合规审查转人工",
                "description": f"session_id={session_id}, compliance_failed=true",
                "priority": "high",
                "category": "compliance_escalation",
            },
        )
        if result.success and isinstance(result.result, dict):
            return result.result.get("ticket_id", "")
        return ""

    @trace_agent_call("supervisor_synthesize")
    async def synthesize_response(self, state: AgentState) -> AgentState:
        """汇总子Agent结果，生成最终回复"""
        if state.get("needs_clarification") and state.get("final_response"):
            final_response = state["final_response"]
        elif not state.get("compliance_passed", True):
            ticket_id = await self._create_escalation_ticket(state)
            base = (
                "抱歉，您的请求涉及敏感内容，已转交人工客服处理。"
            )
            if ticket_id:
                final_response = f"{base}工单编号：{ticket_id}，请留意后续通知。"
            else:
                final_response = f"{base}工单编号已自动生成，请留意后续通知。"
        else:
            sub_results = state.get("sub_results", {})
            result_parts = []
            skip_keys = {"intent_router", "compliance", "_wm_context"}
            for agent_name, result in sub_results.items():
                if agent_name in skip_keys:
                    continue
                if isinstance(result, str) and result:
                    result_parts.append(result)
            final_response = (
                "\n\n".join(result_parts)
                if result_parts
                else "抱歉，暂时无法处理您的请求，请稍后重试。"
            )

        return {
            **state,
            "final_response": final_response,
            "messages": [AIMessage(content=final_response)],
        }


# ─── 路由函数 ───

def route_to_agent(state: AgentState) -> str:
    """根据 intent_router 写入的 intent 分发到对应 Agent 节点"""
    intent = state.get("intent", "knowledge_rag")
    route_map = {
        "knowledge_rag": "knowledge_rag",
        "ticket_handler": "ticket_handler",
        "compliance_checker": "compliance_check",
    }
    return route_map.get(intent, "knowledge_rag")


def route_after_intent(state: AgentState) -> str:
    """Dispatch a routine clarification directly; only security intents enter compliance."""
    if state.get("needs_clarification"):
        return "clarification"
    return route_to_agent(state)


# ─── 构建Graph ───

def create_supervisor_graph(
    llm: ChatOpenAI | None = None,
    working_memory: WorkingMemory | None = None,
    short_term_memory: ShortTermMemory | None = None,
    long_term_memory: LongTermMemory | None = None,
    mcp_server: MCPToolServer | None = None,
    enable_checkpointing: bool = True,
    intent_confidence_threshold: float = 0.70,
    intent_candidate_margin: float = 0.15,
    intent_context_turns: int = 3,
    intent_entity_ttl_turns: int = 5,
    intent_format_repair_enabled: bool = True,
    intent_prompt_version: str = "apple-support-v1",
    rag_query_rewrite_enabled: bool = True,
    rag_llm_rerank_enabled: bool = True,
    compliance_llm_review_enabled: bool = True,
) -> StateGraph:
    """
    构建Supervisor编排的多Agent StateGraph。

    编排顺序与 Java/Go 一致：
    supervisor_route → intent_router → sub-agent → compliance_check → synthesize
    """
    if llm is None:
        llm = create_traced_chat_openai(model=os.getenv("MODEL_NAME", "deepseek-v4-flash"), temperature=0)
    if working_memory is None:
        working_memory = WorkingMemory()

    supervisor = SupervisorNode(llm, working_memory, short_term_memory, mcp_server)
    intent_router = IntentRouterAgent(
        llm,
        confidence_threshold=intent_confidence_threshold,
        candidate_margin=intent_candidate_margin,
        enable_format_repair=intent_format_repair_enabled,
        context_turns=intent_context_turns,
        prompt_version=intent_prompt_version,
    )
    knowledge_agent = KnowledgeRAGAgent(
        llm,
        long_term_memory,
        enable_query_rewrite=rag_query_rewrite_enabled,
        enable_llm_rerank=rag_llm_rerank_enabled,
    )
    ticket_agent = TicketHandlerAgent(llm, mcp_server=mcp_server)
    compliance_agent = ComplianceCheckerAgent(llm, enable_llm_review=compliance_llm_review_enabled)

    async def intent_router_node(state: AgentState) -> AgentState:
        updated = await intent_router.process(state)
        session_id = updated.get("session_id", "default")
        intent = updated.get("intent", "knowledge_rag")

        ir = updated.get("sub_results", {}).get("intent_router", {})
        new_entities = ir.get("entities", {}) or {}

        # 读取已有工作记忆，合并本轮实体并按确认轮次淘汰过期值。
        wm_ctx = supervisor.working_memory.get_context(session_id)
        new_turn = int(wm_ctx.get("turn_count", 0)) + 1
        supervisor.working_memory.merge_entities(session_id, new_entities, confirmed_turn=new_turn)
        accumulated = supervisor.working_memory.get_active_entities(
            session_id,
            ttl_turns=intent_entity_ttl_turns,
            current_turn=new_turn,
        )

        supervisor.working_memory.update(session_id, {
            "last_intent": intent,
            "accumulated_entities": accumulated,
            "turn_count": new_turn,
        })

        # 注入 _wm_context 到 sub_results
        updated_sub = dict(updated.get("sub_results", {}))
        updated_sub["_wm_context"] = {
            "last_intent": intent,
            "accumulated_entities": accumulated,
            "turn_count": new_turn,
        }

        if updated.get("needs_clarification", False):
            return {
                **updated,
                "sub_results": updated_sub,
                "needs_clarification": True,
                "response_mode": "clarification",
                "final_response": build_clarification_message(ir),
            }

        secondary = str(ir.get("secondary", ""))
        if intent == "compliance_checker":
            updated_sub["security_guidance"] = build_security_guidance(secondary)
        return {**updated, "sub_results": updated_sub, "needs_clarification": False}

    graph = StateGraph(AgentState)

    graph.add_node("supervisor_route", supervisor.route_decision)
    graph.add_node("intent_router", intent_router_node)
    graph.add_node("knowledge_rag", knowledge_agent.process)
    graph.add_node("ticket_handler", ticket_agent.process)
    graph.add_node("compliance_check", compliance_agent.process)
    graph.add_node("clarification", supervisor.present_clarification)
    graph.add_node("synthesize", supervisor.synthesize_response)

    graph.set_entry_point("supervisor_route")
    graph.add_edge("supervisor_route", "intent_router")

    graph.add_conditional_edges(
        "intent_router",
        route_after_intent,
        {
            "knowledge_rag": "knowledge_rag",
            "ticket_handler": "ticket_handler",
            "compliance_check": "compliance_check",
            "clarification": "clarification",
        },
    )

    graph.add_edge("clarification", "synthesize")
    graph.add_edge("knowledge_rag", "compliance_check")
    graph.add_edge("ticket_handler", "compliance_check")
    graph.add_edge("compliance_check", "synthesize")
    graph.add_edge("synthesize", END)

    checkpointer = MemorySaver() if enable_checkpointing else None
    compiled = graph.compile(checkpointer=checkpointer)

    return compiled
