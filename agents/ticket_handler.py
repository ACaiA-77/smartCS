"""Apple support ticket handling with validated ticket categories and explicit provider failures."""

from __future__ import annotations

import json
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


class TicketType(str, Enum):
    REPAIR = "repair"
    RETURN_REFUND = "return_refund"
    SUBSCRIPTION_CANCEL = "subscription_cancel"
    COMPLAINT = "complaint"
    HUMAN_ESCALATION = "human_escalation"
    GENERAL = "general"


TICKET_SYSTEM_PROMPT = """你是 Apple 售后工单处理 Agent，只处理明确的售后办理请求。

可创建工单的类型：repair（维修）、return_refund（退货或退款）、subscription_cancel（取消订阅）、
complaint（投诉）、human_escalation（转人工）、general（其他售后）。

以 JSON 返回：
{"action":"create|query", "ticket_type":"...", "priority":"low|medium|high|urgent", "summary":"...", "details":"...", "ticket_id":"可选"}

安全规则：不要要求密码或验证码；不要承诺退款金额、维修结果或处理时效。
"""


class TicketStore:
    """In-process fallback store for development; a provider remains authoritative when configured."""

    def __init__(self):
        self._tickets: dict[str, dict[str, Any]] = {}

    def create(self, ticket_type: str, priority: str, summary: str, details: str, user_id: str) -> dict[str, Any]:
        ticket_id = f"TK-{datetime.now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:6].upper()}"
        timestamp = datetime.now().isoformat()
        ticket = {
            "ticket_id": ticket_id,
            "type": ticket_type,
            "priority": priority,
            "status": TicketStatus.CREATED.value,
            "summary": summary,
            "details": details,
            "user_id": user_id,
            "created_at": timestamp,
            "updated_at": timestamp,
        }
        self._tickets[ticket_id] = ticket
        return ticket

    def query(self, ticket_id: str) -> dict[str, Any] | None:
        return self._tickets.get(ticket_id)

    def query_by_user(self, user_id: str) -> list[dict[str, Any]]:
        return [ticket for ticket in self._tickets.values() if ticket["user_id"] == user_id]

    def update_status(self, ticket_id: str, status: str) -> dict[str, Any] | None:
        ticket = self._tickets.get(ticket_id)
        if ticket:
            ticket["status"] = TicketStatus(status).value
            ticket["updated_at"] = datetime.now().isoformat()
        return ticket


class TicketHandlerAgent:
    def __init__(
        self,
        llm: ChatOpenAI,
        ticket_store: TicketStore | None = None,
        mcp_server: Any | None = None,
    ):
        self.llm = llm
        self.ticket_store = ticket_store or TicketStore()
        self.mcp_server = mcp_server

    @staticmethod
    def _normalize_ticket_type(value: Any) -> TicketType:
        try:
            return TicketType(str(value))
        except ValueError:
            return TicketType.GENERAL

    @staticmethod
    def _normalize_priority(value: Any) -> TicketPriority:
        try:
            return TicketPriority(str(value))
        except ValueError:
            return TicketPriority.MEDIUM

    @trace_agent_call("ticket_analyze")
    async def analyze_request(self, user_message: str) -> dict[str, Any]:
        response = await self.llm.ainvoke(
            [SystemMessage(content=TICKET_SYSTEM_PROMPT), HumanMessage(content=f"用户消息: {user_message}")]
        )
        try:
            result = json.loads(response.content)
        except (TypeError, json.JSONDecodeError):
            result = {}
        return {
            "action": "query" if result.get("action") == "query" else "create",
            "ticket_type": self._normalize_ticket_type(result.get("ticket_type")).value,
            "priority": self._normalize_priority(result.get("priority")).value,
            "summary": str(result.get("summary") or user_message[:100]),
            "details": str(result.get("details") or user_message),
            "ticket_id": str(result.get("ticket_id") or ""),
        }

    @trace_agent_call("ticket_create")
    async def create_ticket(self, ticket_info: dict[str, Any], user_id: str) -> str:
        ticket_type = self._normalize_ticket_type(ticket_info.get("ticket_type")).value
        priority = self._normalize_priority(ticket_info.get("priority")).value
        summary = str(ticket_info.get("summary") or "Apple 售后请求")
        details = str(ticket_info.get("details") or summary)
        external_ticket_id = ""

        if self.mcp_server is not None:
            provider_result = await self.mcp_server.call_tool(
                "ticket_create",
                {"title": summary, "description": details, "priority": priority, "category": ticket_type},
            )
            if not provider_result.success or not isinstance(provider_result.result, dict):
                return f"暂未能创建工单：{provider_result.error or '工单服务不可用'}。请稍后重试或联系 Apple 官方支持。"
            external_ticket_id = str(provider_result.result.get("ticket_id") or "")
            if not external_ticket_id:
                return "暂未能创建工单：工单服务未返回工单号。请稍后重试或联系 Apple 官方支持。"

        ticket = self.ticket_store.create(ticket_type, priority, summary, details, user_id)
        if external_ticket_id:
            ticket["ticket_id"] = external_ticket_id
            self.ticket_store._tickets.pop(next(key for key, value in self.ticket_store._tickets.items() if value is ticket), None)
            self.ticket_store._tickets[external_ticket_id] = ticket

        priority_label = {"low": "普通", "medium": "中等", "high": "高", "urgent": "紧急"}[priority]
        return (
            "工单已创建成功！\n\n"
            f"📋 工单号: {ticket['ticket_id']}\n"
            f"📝 类型: {ticket['type']}\n"
            f"⚡ 优先级: {priority_label}\n"
            f"📄 摘要: {ticket['summary']}\n"
            f"🕐 创建时间: {ticket['created_at']}\n\n"
            "请保存工单号，以便后续查询。"
        )

    @trace_agent_call("ticket_order_query")
    async def query_order(self, order_id: str, user_id: str) -> str:
        if self.mcp_server is None:
            return f"订单 {order_id or '未知'} 查询服务暂不可用，请联系 Apple 官方支持。"
        result = await self.mcp_server.call_tool("order_query", {"order_id": order_id, "user_id": user_id})
        if not result.success or not isinstance(result.result, dict):
            return f"订单查询失败：{result.error or '未知错误'}"
        order = result.result
        status = {"shipped": "已发货", "delivered": "已送达", "pending": "待处理", "processing": "处理中"}.get(
            order.get("status", ""), order.get("status", "未知")
        )
        return (
            "订单查询结果：\n\n"
            f"📦 订单号: {order.get('order_id', order_id)}\n"
            f"📊 状态: {status}\n"
            f"🛍️ 商品: {order.get('product', '—')}\n"
            f"🕐 下单时间: {order.get('created_at', '—')}"
        )

    @trace_agent_call("ticket_query")
    async def query_ticket(self, ticket_id: str) -> str:
        ticket = self.ticket_store.query(ticket_id)
        if not ticket:
            return f"未找到工单号 {ticket_id}，请确认工单号是否正确。"
        status = {
            "created": "已创建", "processing": "处理中", "pending_review": "待审核",
            "resolved": "已解决", "closed": "已关闭", "escalated": "已升级",
        }.get(ticket["status"], ticket["status"])
        return (
            "工单查询结果：\n\n"
            f"📋 工单号: {ticket['ticket_id']}\n📊 状态: {status}\n"
            f"📝 类型: {ticket['type']}\n📄 摘要: {ticket['summary']}\n"
            f"🕐 创建时间: {ticket['created_at']}\n🔄 更新时间: {ticket['updated_at']}"
        )

    @staticmethod
    def _repair_booking_details_prompt(entities: dict[str, Any]) -> str:
        product = str(entities.get("product") or entities.get("device_model") or "您的设备")
        return (
            f"好的，我可以协助您预约 {product} 维修。"
            "请告诉我您所在的城市或地区，以及设备目前是否能够正常开机；"
            "如果您希望到店服务，也可以说明方便的时间。"
            "为保护账户安全，请不要提供 Apple ID 密码或验证码。"
        )

    @staticmethod
    def _extract_entity_id(entities: dict[str, str]) -> str | None:
        for key in ("ticket_id", "order_id", "工单号", "订单号"):
            if entities.get(key):
                return entities[key]
        return None

    @trace_agent_call("ticket_handler_process")
    async def process(self, state: dict[str, Any]) -> dict[str, Any]:
        messages = state.get("messages", [])
        if not messages:
            return state
        user_id = state.get("user_id", "anonymous")
        intent_info = state.get("sub_results", {}).get("intent_router", {})
        entities = dict(intent_info.get("entities", {}) or {})
        for key, value in (state.get("sub_results", {}).get("_wm_context", {}).get("accumulated_entities", {}) or {}).items():
            entities.setdefault(key, value)

        secondary = str(intent_info.get("secondary", ""))
        if secondary == "repair_request" and not entities.get("region"):
            result = self._repair_booking_details_prompt(entities)
            return {
                **state,
                "current_agent": "ticket_handler",
                "response_mode": "collect_ticket_details",
                "sub_results": {**state.get("sub_results", {}), "ticket_handler": result},
            }

        ticket_info = await self.analyze_request(messages[-1].content)
        order_id = str(entities.get("order_id") or "")
        ticket_id = str(entities.get("ticket_id") or ticket_info.get("ticket_id") or "")
        if secondary == "order_query" and order_id:
            result = await self.query_order(order_id, user_id)
        elif ticket_info["action"] == "query" and ticket_id:
            result = await self.query_ticket(ticket_id)
        elif ticket_info["action"] == "query" and order_id:
            result = await self.query_order(order_id, user_id)
        else:
            result = await self.create_ticket(ticket_info, user_id)

        return {
            **state,
            "current_agent": "ticket_handler",
            "response_mode": "ticket_execution",
            "sub_results": {**state.get("sub_results", {}), "ticket_handler": result},
        }
