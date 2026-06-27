"""
工单处理Agent — 工单CRUD与流转
负责创建、查询、更新工单，对接工单系统，处理退款/理赔/开户等业务办理类需求。
通过MCP工具协议调用外部工单系统。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from tracing.otel_config import trace_agent_call


class TicketStatus(str, Enum):
    CREATED = "created"
    PROCESSING = "processing"
    PENDING_REVIEW = "pending_review"
    RESOLVED = "resolved"
    CLOSED = "closed"
    ESCALATED = "escalated"


class TicketPriority(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    URGENT = "urgent"


TICKET_SYSTEM_PROMPT = """你是一个专业的工单处理Agent，负责处理客户的业务办理请求。

你的职责：
1. 分析用户需求，判断是否需要创建工单
2. 提取工单关键信息（类型、优先级、描述）
3. 创建工单并返回工单号
4. 查询现有工单状态

工单类型：
- refund: 退款申请
- claim: 理赔申请
- account_open: 开户申请
- account_change: 账户变更
- complaint: 投诉工单
- general: 通用工单

优先级判断规则：
- urgent: 资金安全、账户被盗
- high: 退款超时、理赔争议
- medium: 常规业务办理
- low: 信息咨询类

请以JSON格式返回工单信息：
{
    "action": "create|query|update",
    "ticket_type": "refund|claim|account_open|...",
    "priority": "low|medium|high|urgent",
    "summary": "工单摘要",
    "details": "详细描述"
}
"""


class TicketStore:
    """内存工单存储（生产环境应替换为数据库）"""

    def __init__(self):
        self._tickets: dict[str, dict] = {}

    def create(self, ticket_type: str, priority: str, summary: str, details: str, user_id: str) -> dict:
        ticket_id = f"TK-{datetime.now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:6].upper()}"
        ticket = {
            "ticket_id": ticket_id,
            "type": ticket_type,
            "priority": priority,
            "status": TicketStatus.CREATED.value,
            "summary": summary,
            "details": details,
            "user_id": user_id,
            "created_at": datetime.now().isoformat(),
            "updated_at": datetime.now().isoformat(),
        }
        self._tickets[ticket_id] = ticket
        return ticket

    def query(self, ticket_id: str) -> dict | None:
        return self._tickets.get(ticket_id)

    def query_by_user(self, user_id: str) -> list[dict]:
        return [t for t in self._tickets.values() if t["user_id"] == user_id]

    def update_status(self, ticket_id: str, status: str) -> dict | None:
        ticket = self._tickets.get(ticket_id)
        if ticket:
            ticket["status"] = status
            ticket["updated_at"] = datetime.now().isoformat()
        return ticket


class TicketHandlerAgent:
    """工单处理Agent"""

    def __init__(
        self,
        llm: ChatOpenAI,
        ticket_store: TicketStore | None = None,
        mcp_server: Any | None = None,
    ):
        self.llm = llm
        self.ticket_store = ticket_store or TicketStore()
        self.mcp_server = mcp_server

    @trace_agent_call("ticket_analyze")
    async def analyze_request(self, user_message: str) -> dict:
        """分析用户需求，提取工单信息"""
        messages = [
            SystemMessage(content=TICKET_SYSTEM_PROMPT),
            HumanMessage(content=f"用户消息: {user_message}"),
        ]

        response = await self.llm.ainvoke(messages)

        import json
        try:
            return json.loads(response.content)
        except json.JSONDecodeError:
            return {
                "action": "create",
                "ticket_type": "general",
                "priority": "medium",
                "summary": user_message[:100],
                "details": user_message,
            }

    @trace_agent_call("ticket_create")
    async def create_ticket(self, ticket_info: dict, user_id: str) -> str:
        """创建工单（优先 MCP ticket_create，本地 TicketStore 同步备份）"""
        summary = ticket_info.get("summary", "")
        details = ticket_info.get("details", "")
        priority = ticket_info.get("priority", "medium")
        ticket_type = ticket_info.get("ticket_type", "general")
        ticket_id = ""

        if self.mcp_server is not None:
            mcp_result = await self.mcp_server.call_tool(
                "ticket_create",
                {
                    "title": summary or details[:80],
                    "description": details or summary,
                    "priority": priority,
                    "category": ticket_type,
                },
            )
            if mcp_result.success and isinstance(mcp_result.result, dict):
                ticket_id = mcp_result.result.get("ticket_id", "")

        ticket = self.ticket_store.create(
            ticket_type=ticket_type,
            priority=priority,
            summary=summary,
            details=details,
            user_id=user_id,
        )
        if ticket_id:
            ticket["ticket_id"] = ticket_id

        priority_label = {
            "low": "普通", "medium": "中等", "high": "高", "urgent": "紧急"
        }.get(ticket["priority"], "中等")

        return (
            f"工单已创建成功！\n\n"
            f"📋 工单号: {ticket['ticket_id']}\n"
            f"📝 类型: {ticket['type']}\n"
            f"⚡ 优先级: {priority_label}\n"
            f"📄 摘要: {ticket['summary']}\n"
            f"🕐 创建时间: {ticket['created_at']}\n\n"
            f"我们将尽快处理您的请求，请保存好工单号以便后续查询。"
        )

    @trace_agent_call("ticket_order_query")
    async def query_order(self, order_id: str, user_id: str) -> str:
        """通过 MCP order_query 查询订单"""
        if self.mcp_server is None:
            return f"订单 {order_id or '未知'} 查询服务暂不可用，请联系人工客服。"

        mcp_result = await self.mcp_server.call_tool(
            "order_query",
            {"order_id": order_id or "", "user_id": user_id},
        )
        if not mcp_result.success or not isinstance(mcp_result.result, dict):
            return f"订单查询失败：{mcp_result.error or '未知错误'}"

        order = mcp_result.result
        status_map = {
            "shipped": "已发货",
            "delivered": "已送达",
            "pending": "待处理",
            "processing": "处理中",
        }
        status = status_map.get(order.get("status", ""), order.get("status", "未知"))
        return (
            f"订单查询结果：\n\n"
            f"📦 订单号: {order.get('order_id', order_id)}\n"
            f"📊 状态: {status}\n"
            f"💰 金额: {order.get('amount', '—')}\n"
            f"🛍️ 商品: {order.get('product', '—')}\n"
            f"🕐 下单时间: {order.get('created_at', '—')}"
        )

    @trace_agent_call("ticket_query")
    async def query_ticket(self, ticket_id: str) -> str:
        """查询工单状态"""
        ticket = self.ticket_store.query(ticket_id)
        if not ticket:
            return f"未找到工单号 {ticket_id}，请确认工单号是否正确。"

        status_label = {
            "created": "已创建",
            "processing": "处理中",
            "pending_review": "待审核",
            "resolved": "已解决",
            "closed": "已关闭",
            "escalated": "已升级",
        }.get(ticket["status"], ticket["status"])

        return (
            f"工单查询结果：\n\n"
            f"📋 工单号: {ticket['ticket_id']}\n"
            f"📊 状态: {status_label}\n"
            f"📝 类型: {ticket['type']}\n"
            f"📄 摘要: {ticket['summary']}\n"
            f"🕐 创建时间: {ticket['created_at']}\n"
            f"🔄 更新时间: {ticket['updated_at']}"
        )

    @staticmethod
    def _extract_entity_id(entities: dict[str, str]) -> str | None:
        """从 intent_router 实体中抽取订单号/工单号"""
        for key in ("ticket_id", "order_id", "工单号", "订单号"):
            val = entities.get(key)
            if val:
                return val
        return None

    @trace_agent_call("ticket_handler_process")
    async def process(self, state: dict[str, Any]) -> dict[str, Any]:
        """作为Graph节点处理状态"""
        messages = state.get("messages", [])
        user_id = state.get("user_id", "anonymous")

        if not messages:
            return state

        last_message = messages[-1].content
        intent_info = state.get("sub_results", {}).get("intent_router", {})
        entities = intent_info.get("entities", {}) or {}

        # 从工作记忆累积实体中回退补全
        wm = state.get("sub_results", {}).get("_wm_context", {})
        accumulated = wm.get("accumulated_entities", {}) or {}
        for key, val in accumulated.items():
            if key not in entities or not entities[key]:
                entities[key] = val

        entity_id = self._extract_entity_id(entities)

        ticket_info = await self.analyze_request(last_message)
        if entity_id:
            ticket_info.setdefault("ticket_id", entity_id)

        action = ticket_info.get("action", "create")
        query_id = ticket_info.get("ticket_id") or entity_id
        secondary = intent_info.get("secondary", "")

        if secondary == "order_query" or (entity_id and entity_id.startswith("ORD")):
            result = await self.query_order(entity_id or query_id or "", user_id)
        elif action == "query" and query_id:
            result = await self.query_ticket(query_id)
        else:
            result = await self.create_ticket(ticket_info, user_id)

        return {
            **state,
            "sub_results": {
                **state.get("sub_results", {}),
                "ticket_handler": result,
            },
        }
