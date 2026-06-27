"""
Supervisor编排Agent — 中央协调者
负责接收用户请求，根据意图路由到对应子Agent，汇总结果返回。
采用LangGraph StateGraph实现串行编排；MemorySaver + thread_id 已配置 Checkpoint。
"""

from __future__ import annotations

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
    """intent_router 完成后：低置信度跳过 sub-agent 仍走合规，否则按 intent 分发"""
    if state.get("needs_clarification"):
        return "compliance_check"
    return route_to_agent(state)


# ─── 构建Graph ───

def create_supervisor_graph(
    llm: ChatOpenAI | None = None,
    working_memory: WorkingMemory | None = None,
    short_term_memory: ShortTermMemory | None = None,
    long_term_memory: LongTermMemory | None = None,
    mcp_server: MCPToolServer | None = None,
    enable_checkpointing: bool = True,
) -> StateGraph:
    """
    构建Supervisor编排的多Agent StateGraph。

    编排顺序与 Java/Go 一致：
    supervisor_route → intent_router → sub-agent → compliance_check → synthesize
    """
    if llm is None:
        llm = create_traced_chat_openai(model="gpt-4o", temperature=0)
    if working_memory is None:
        working_memory = WorkingMemory()

    supervisor = SupervisorNode(llm, working_memory, short_term_memory, mcp_server)
    intent_router = IntentRouterAgent(llm)
    knowledge_agent = KnowledgeRAGAgent(llm, long_term_memory)
    ticket_agent = TicketHandlerAgent(llm, mcp_server=mcp_server)
    compliance_agent = ComplianceCheckerAgent(llm)

    async def intent_router_node(state: AgentState) -> AgentState:
        updated = await intent_router.process(state)
        session_id = updated.get("session_id", "default")
        intent = updated.get("intent", "knowledge_rag")

        ir = updated.get("sub_results", {}).get("intent_router", {})
        new_entities = ir.get("entities", {}) or {}

        # 读取已有工作记忆，合并实体
        wm_ctx = supervisor.working_memory.get_context(session_id)
        accumulated = dict(wm_ctx.get("accumulated_entities", {}))
        accumulated.update(new_entities)
        new_turn = wm_ctx.get("turn_count", 0) + 1

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

        confidence = ir.get("confidence", 1.0)
        if confidence < 0.7:
            return {
                **updated,
                "sub_results": updated_sub,
                "needs_clarification": True,
                "final_response": (
                    "抱歉，我还不太确定您的具体需求。"
                    "您是想咨询产品信息、查询订单，还是办理退款/开户？"
                    "请补充说明，我来帮您处理。"
                ),
            }
        return {**updated, "sub_results": updated_sub, "needs_clarification": False}

    graph = StateGraph(AgentState)

    graph.add_node("supervisor_route", supervisor.route_decision)
    graph.add_node("intent_router", intent_router_node)
    graph.add_node("knowledge_rag", knowledge_agent.process)
    graph.add_node("ticket_handler", ticket_agent.process)
    graph.add_node("compliance_check", compliance_agent.process)
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
        },
    )

    graph.add_edge("knowledge_rag", "compliance_check")
    graph.add_edge("ticket_handler", "compliance_check")
    graph.add_edge("compliance_check", "synthesize")
    graph.add_edge("synthesize", END)

    checkpointer = MemorySaver() if enable_checkpointing else None
    compiled = graph.compile(checkpointer=checkpointer)

    return compiled
