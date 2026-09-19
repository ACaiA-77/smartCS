"""
工单处理Agent — 工单CRUD与流转
负责创建、查询、更新工单，对接工单系统，处理退款/理赔/开户等业务办理类需求。
通过MCP工具协议调用外部工单系统。
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from tracing.otel_config import trace_agent_call
from mcp.tool_execution import ToolExecutionContext, ToolExecutor
from tickets.service import canonical_ticket_payload_hash
from checkpoint.models import active_checkpoint


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


class TicketHandlerAgent:
    """工单处理Agent"""

    def __init__(
        self,
        llm: ChatOpenAI,
        mcp_server: Any | None = None,
        tool_executor: ToolExecutor | None = None,
    ):
        self.llm = llm
        self.tool_executor = tool_executor
        # Kept for constructor compatibility; business execution never falls
        # back to the raw MCP transport.
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
    async def create_ticket(self, ticket_info: dict, user_id: str, session_id: str = "default") -> str:
        """通过共享执行层创建并持久化工单。"""
        summary = ticket_info.get("summary", "")
        details = ticket_info.get("details", "")
        priority = ticket_info.get("priority", "medium")
        ticket_type = ticket_info.get("ticket_type", "general")
        if self.tool_executor is None:
            return "工单创建服务暂不可用，请联系人工客服。"

        arguments = {
            "title": summary or details[:80],
            "description": details or summary,
            "priority": priority,
            "category": ticket_type,
        }
        request_hash = canonical_ticket_payload_hash(
            user_id=user_id,
            title=arguments["title"],
            description=arguments["description"],
            priority=priority,
            category=ticket_type,
        )
        normalized_ticket_type = " ".join(str(ticket_type or "general").split()).lower() or "general"
        client_request_id = f"ticket-client:{session_id}:{normalized_ticket_type}:{request_hash}"
        key = f"ticket:{session_id}:{normalized_ticket_type}:{request_hash}"
        arguments.update(
            {
                "user_id": str(user_id).strip(),
                "client_request_id": client_request_id,
                "request_payload_hash": request_hash,
            }
        )
        result = await self.tool_executor.execute(
            "ticket_create",
            arguments,
            ToolExecutionContext(confirmed=True, idempotency_key=key),
        )
        if not result.success or not isinstance(result.result, dict):
            return f"工单创建失败：{result.error or '未知错误'}"
        if result.result.get("success") is False:
            return f"工单创建失败：{result.result.get('reason_code', '未知错误')}"

        ticket_id = str(result.result.get("ticket_id", "")).strip()
        if not ticket_id:
            return "工单创建失败：工具未返回工单号"

        priority_label = {
            "low": "普通", "medium": "中等", "high": "高", "urgent": "紧急"
        }.get(result.result.get("priority", priority), "中等")

        return (
            f"工单已创建成功！\n\n"
            f"📋 工单号: {ticket_id}\n"
            f"📝 类型: {result.result.get('type', ticket_type)}\n"
            f"⚡ 优先级: {priority_label}\n"
            f"📄 摘要: {result.result.get('summary', summary)}\n"
            f"🕐 创建时间: {result.result.get('created_at', '—')}\n\n"
            f"我们将尽快处理您的请求，请保存好工单号以便后续查询。"
        )

    @trace_agent_call("ticket_order_query")
    async def query_order(self, order_id: str, user_id: str) -> str:
        """通过 MCP order_query 查询订单"""
        if not order_id or not order_id.strip():
            return "请提供演示订单号，例如：查询订单 ORD-20260801-0001。"
        if self.tool_executor is None:
            return f"订单 {order_id} 查询服务暂不可用，请联系人工客服。"

        result = await self.tool_executor.execute(
            "order_query",
            {"order_id": order_id or "", "user_id": user_id},
            ToolExecutionContext(),
        )
        if not result.success or not isinstance(result.result, dict):
            return f"订单查询失败：{result.error or '未知错误'}"

        order = result.result
        if not order.get("found", True):
            return (
                f"未找到本地演示订单 {order.get('order_id', order_id)}。"
                "请确认订单号，例如：查询订单 ORD-20260801-0001。"
            )

        status_map = {
            "shipped": "已发货",
            "delivered": "已送达",
            "pending": "待处理",
            "processing": "处理中",
            "unavailable": "公开数据未提供",
        }
        status = order.get("status_label") or status_map.get(
            order.get("status", ""),
            order.get("status", "未知"),
        )
        return (
            f"订单查询结果（{order.get('data_source', '未知数据源')}）：\n\n"
            f"📦 订单号: {order.get('order_id', order_id)}\n"
            f"📊 状态: {status}\n"
            f"💳 支付: {order.get('payment_status_label', '—')}\n"
            f"💰 实付金额: {order.get('amount', '—')} 元\n"
            f"🛍️ 商品: {order.get('product', '—')}\n"
            f"🚚 物流: {order.get('courier_company') or '暂未发货'}"
            f"{('，运单号 ' + order['tracking_number']) if order.get('tracking_number') else ''}\n"
            f"🛠️ 售后: {order.get('after_sale_status_label', '无')}\n"
            f"🕐 下单时间: {order.get('created_at', '—')}\n"
            "ℹ️ 说明: 本地国内电商演示数据，不代表真实平台订单。"
        )

    @trace_agent_call("ticket_query")
    async def query_ticket(self, ticket_id: str, user_id: str = "anonymous") -> str:
        """通过共享执行层查询当前用户的工单状态。"""
        if self.tool_executor is None:
            return "工单查询服务暂不可用，请联系人工客服。"
        result = await self.tool_executor.execute(
            "ticket_query",
            {"ticket_id": ticket_id, "user_id": user_id},
        )
        if not result.success or not isinstance(result.result, dict):
            return f"工单查询失败：{result.error or '未知错误'}"
        ticket = result.result
        if not ticket.get("success", True):
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
            f"📝 类型: {ticket.get('type', ticket.get('ticket_type', 'general'))}\n"
            f"📄 摘要: {ticket.get('summary', ticket.get('title', ''))}\n"
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

    @staticmethod
    def _should_backfill_entity(key: str, secondary: str, action: str) -> bool:
        """Only reuse remembered business IDs when the current turn asks for them."""
        normalized_key = key.lower()
        if "order" in normalized_key or "璁㈠崟" in key:
            return secondary == "order_query"
        if "ticket" in normalized_key or "宸ュ崟" in key:
            return action == "query" or secondary in {"ticket_query", "ticket_status"}
        return action == "query"

    @staticmethod
    def _is_order_switch_request(user_message: str) -> bool:
        return bool(re.search(r"换一个|换一笔|另一个|另一笔|再看|再查", user_message))

    @staticmethod
    def _has_explicit_create_consent(user_message: str) -> bool:
        """LLM intent alone cannot authorize a ticket-creating side effect."""
        normalized = re.sub(r"\s+", "", str(user_message))
        if any(
            marker in normalized
            for marker in (
                "怎么",
                "如何",
                "怎样",
                "流程",
                "步骤",
                "规则",
                "政策",
                "条件",
                "入口",
                "在哪里",
                "需要什么",
                "需要哪些",
                "怎么办",
                "是什么",
                "有哪些要求",
            )
        ):
            return False
        if "工单" in normalized and any(
            phrase in normalized for phrase in ("帮我创建", "我要创建", "请创建", "提交工单", "发起工单")
        ):
            return True
        return any(
            phrase in normalized
            for phrase in (
                "我要投诉",
                "帮我投诉",
                "我要申请理赔",
                "帮我申请理赔",
                "我要开户",
                "帮我办理开户",
                "提交投诉",
                "提交申请",
                "创建工单",
                "发起申请",
            )
        )

    @staticmethod
    def _create_consent_prompt() -> str:
        return (
            "如果需要我代为创建工单，请明确告诉我“帮我创建投诉工单”或“我要申请理赔”。"
        )

    @trace_agent_call("ticket_handler_process")
    async def process(self, state: dict[str, Any]) -> dict[str, Any]:
        """作为Graph节点处理状态"""
        messages = state.get("messages", [])
        user_id = state.get("user_id", "anonymous")

        if not messages:
            return state

        last_message = messages[-1].content
        intent_info = state.get("sub_results", {}).get("intent_router", {})
        entities = dict(intent_info.get("entities", {}) or {})
        secondary = intent_info.get("secondary", "")

        # 从工作记忆累积实体中回退补全
        context = state.get("sub_results", {}).get("_session_context", {})
        accumulated = context.get("accumulated_entities", {}) or {}
        is_switch = self._is_order_switch_request(last_message)

        # 订单查询已经由意图路由确定，不再调用工单分析模型，避免无意义延迟和 TK 编号污染。
        if secondary == "order_query":
            explicit_order = re.search(r"(?<![A-Z0-9])ORD[-_][A-Z0-9-]+", last_message, re.IGNORECASE)
            if explicit_order:
                order_id = explicit_order.group(0).replace("_", "-").upper()
            elif is_switch:
                order_id = ""
            else:
                order_id = accumulated.get("order_id", "")
            result = await self.query_order(order_id, user_id)
            return {
                **state,
                "sub_results": {
                    **state.get("sub_results", {}),
                    "ticket_handler": result,
                },
            }

        checkpoint = active_checkpoint.get()
        ticket_info = (
            await checkpoint.ticket_plan(lambda: self.analyze_request(last_message))
            if checkpoint is not None else await self.analyze_request(last_message)
        )
        action = ticket_info.get("action", "create")
        for key, val in accumulated.items():
            if (
                (key not in entities or not entities[key])
                and self._should_backfill_entity(key, secondary, action)
                and not (
                    "order" in key.lower()
                    and is_switch
                )
            ):
                entities[key] = val

        entity_id = self._extract_entity_id(entities)

        if entity_id:
            ticket_info.setdefault("ticket_id", entity_id)

        query_id = ticket_info.get("ticket_id") or entity_id

        if entity_id and entity_id.upper().startswith("ORD"):
            result = await self.query_order(entity_id, user_id)
        elif action == "query" and query_id:
            result = await self.query_ticket(query_id, user_id)
        elif action == "query":
            result = "请提供工单号，我才能查询工单状态。"
        elif action == "update":
            result = "当前暂不支持更新工单，请说明需要办理的新业务。"
        elif action == "create" and self._has_explicit_create_consent(last_message):
            result = await self.create_ticket(ticket_info, user_id, state.get("session_id", "default"))
        else:
            result = self._create_consent_prompt()

        return {
            **state,
            "sub_results": {
                **state.get("sub_results", {}),
                "ticket_handler": result,
            },
        }
