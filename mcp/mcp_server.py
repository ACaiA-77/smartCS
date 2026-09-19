"""
MCP工具协议服务端 — Model Context Protocol实现
遵循Anthropic MCP标准，通过JSON-RPC 2.0提供工具注册/发现/调用能力。
支持动态工具扩展，Agent通过统一接口调用外部系统。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable
from datetime import datetime

from auth.context import current_user


def customer_tool_arguments(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Bind customer tools to request authentication, not model-supplied identity."""
    user = current_user.get()
    if user is None or name not in {
        "order_query", "refund_evaluate", "refund_create", "ticket_create", "ticket_query",
    }:
        return arguments
    if "user_id" in arguments and arguments["user_id"] != user.business_user_id:
        raise PermissionError("tool user_id does not match authenticated user")
    return {**arguments, "user_id": user.business_user_id}


@dataclass
class ToolDefinition:
    """MCP工具定义"""
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[..., Awaitable[Any]]
    category: str = "general"
    requires_auth: bool = False
    operation_type: str = "read"
    risk_level: str = "low"
    requires_confirmation: bool = False
    retryable: bool = True
    recovery_fields: tuple[str, ...] = ()


@dataclass
class ToolCallResult:
    """工具调用结果"""
    tool_name: str
    success: bool
    result: Any = None
    error: str | None = None
    duration_ms: float = 0.0
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())


class MCPToolServer:
    """
    MCP工具服务端。

    实现 Model Context Protocol 的核心功能：
    1. 工具注册 (Tool Registration)
    2. 工具发现 (Tool Discovery) - Agent可查询可用工具列表
    3. 工具调用 (Tool Invocation) -  通过JSON-RPC 2.0协议调用
    4. 结果返回 (Result Delivery)

    遵循MCP规范：
    - 使用JSON-RPC 2.0消息格式
    - 支持工具的inputSchema声明
    - 提供标准化的错误码
    """

    def __init__(self):
        self._tools: dict[str, ToolDefinition] = {}
        self._call_log: list[ToolCallResult] = []

    def register_tool(self, tool: ToolDefinition) -> None:
        """注册一个MCP工具"""
        self._tools[tool.name] = tool

    def get_tool(self, name: str) -> ToolDefinition | None:
        """按名称返回工具定义，供执行层使用。"""
        return self._tools.get(name)

    def get_tool_definition(self, name: str) -> ToolDefinition | None:
        """get_tool 的语义化别名。"""
        return self.get_tool(name)

    def register(
        self,
        name: str,
        description: str,
        input_schema: dict[str, Any],
        category: str = "general",
        requires_auth: bool = False,
        operation_type: str = "read",
        risk_level: str = "low",
        requires_confirmation: bool = False,
        retryable: bool = True,
        recovery_fields: tuple[str, ...] = (),
    ) -> Callable:
        """工具注册装饰器"""
        def decorator(func: Callable[..., Awaitable[Any]]) -> Callable:
            tool = ToolDefinition(
                name=name,
                description=description,
                input_schema=input_schema,
                handler=func,
                category=category,
                requires_auth=requires_auth,
                operation_type=operation_type,
                risk_level=risk_level,
                requires_confirmation=requires_confirmation,
                retryable=retryable,
                recovery_fields=recovery_fields,
            )
            self._tools[name] = tool
            return func
        return decorator

    def list_tools(self, category: str | None = None) -> list[dict]:
        """
        工具发现：列出所有可用工具。
        对应MCP的 tools/list 方法。
        """
        tools = []
        for tool in self._tools.values():
            if category and tool.category != category:
                continue
            tools.append({
                "name": tool.name,
                "description": tool.description,
                "inputSchema": tool.input_schema,
                "category": tool.category,
                "operationType": tool.operation_type,
                "riskLevel": tool.risk_level,
                "requiresConfirmation": tool.requires_confirmation,
                "retryable": tool.retryable,
            })
        return tools

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> ToolCallResult:
        """
        工具调用：执行指定工具。
        对应MCP的 tools/call 方法。
        """
        import time

        tool = self._tools.get(name)
        if tool is None:
            result = ToolCallResult(
                tool_name=name,
                success=False,
                error=f"Tool '{name}' not found. Available: {list(self._tools.keys())}",
            )
            self._call_log.append(result)
            return result

        start = time.time()
        try:
            arguments = customer_tool_arguments(name, arguments)
            output = await tool.handler(**arguments)
            duration_ms = (time.time() - start) * 1000

            result = ToolCallResult(
                tool_name=name,
                success=True,
                result=output,
                duration_ms=duration_ms,
            )
        except Exception as e:
            duration_ms = (time.time() - start) * 1000
            result = ToolCallResult(
                tool_name=name,
                success=False,
                error=str(e),
                duration_ms=duration_ms,
            )

        self._call_log.append(result)
        return result

    async def handle_jsonrpc(self, request: dict) -> dict:
        """
        处理JSON-RPC 2.0请求。
        MCP协议传输层实现。
        """
        method = request.get("method", "")
        params = request.get("params", {})
        req_id = request.get("id", 1)

        try:
            if method == "tools/list":
                result = self.list_tools(category=params.get("category"))
            elif method == "tools/call":
                tool_name = params.get("name", "")
                arguments = params.get("arguments", {})
                call_result = await self.call_tool(tool_name, arguments)
                result = {
                    "success": call_result.success,
                    "result": call_result.result,
                    "error": call_result.error,
                }
            elif method == "ping":
                result = {"status": "ok"}
            else:
                return {
                    "jsonrpc": "2.0",
                    "error": {"code": -32601, "message": f"Method not found: {method}"},
                    "id": req_id,
                }

            return {"jsonrpc": "2.0", "result": result, "id": req_id}

        except Exception as e:
            return {
                "jsonrpc": "2.0",
                "error": {"code": -32603, "message": str(e)},
                "id": req_id,
            }

    def get_call_log(self, last_n: int = 100) -> list[dict]:
        """获取最近的工具调用日志"""
        return [
            {
                "tool": r.tool_name,
                "success": r.success,
                "duration_ms": r.duration_ms,
                "timestamp": r.timestamp,
                "error": r.error,
            }
            for r in self._call_log[-last_n:]
        ]


def create_default_tools(
    server: MCPToolServer,
    long_term_memory=None,
    order_repository=None,
    refund_service=None,
    ticket_service=None,
) -> MCPToolServer:
    """注册默认的MCP工具集"""

    if refund_service is None and order_repository is not None:
        from refunds.service import RefundService

        refund_service = RefundService(order_repository)
    if ticket_service is None and order_repository is not None:
        from tickets.service import TicketService

        ticket_service = TicketService(order_repository)

    @server.register(
        name="order_query",
        description="查询订单信息，支持按订单号或用户ID查询",
        input_schema={
            "type": "object",
            "properties": {
                "order_id": {"type": "string", "description": "订单号"},
                "user_id": {"type": "string", "description": "用户ID"},
            },
            "required": ["order_id"],
        },
        category="order",
    )
    async def order_query(order_id: str = "", user_id: str = "") -> dict:
        normalized_order_id = str(order_id).strip()
        if not normalized_order_id:
            raise ValueError("order_id must not be empty")

        data_source = getattr(order_repository, "DEMO_DATA_SOURCE", "SQLite 本地国内电商演示数据")
        if order_repository is not None:
            normalized_user_id = "" if user_id is None else str(user_id).strip()
            order = (
                order_repository.get_order_for_user(normalized_order_id, normalized_user_id)
                if normalized_user_id
                else order_repository.get_order(normalized_order_id)
            )
            if order is not None:
                return {
                    "found": True,
                    "data_source": data_source,
                    "order_id": normalized_order_id,
                    "status": order["status"],
                    "status_label": order["status_label"],
                    "payment_status": order["payment_status"],
                    "payment_status_label": order["payment_status_label"],
                    "amount": order["pay_amount"],
                    "original_amount": order["original_amount"],
                    "discount_amount": order["discount_amount"],
                    "product": order["product"],
                    "products": order["items"],
                    "recipient_name_masked": order["recipient_name_masked"],
                    "recipient_phone_masked": order["recipient_phone_masked"],
                    "city": order["city"],
                    "courier_company": order["courier_company"],
                    "tracking_number": order["tracking_number"],
                    "shipped_at": order["shipped_at"],
                    "delivered_at": order["delivered_at"],
                    "after_sale_status": order["after_sale_status"],
                    "after_sale_status_label": order["after_sale_status_label"],
                    "created_at": order["created_at"],
                    "payment": order["payment"],
                    "shipment": order["shipment"],
                    "refunds": order["refunds"],
                }
        return {
            "found": False,
            "order_id": normalized_order_id,
            "data_source": data_source,
            "message": "本地演示订单不存在",
        }

    @server.register(
        name="refund_evaluate",
        description="评估订单是否符合退款条件",
        input_schema={
            "type": "object",
            "properties": {
                "order_id": {"type": "string", "description": "订单号"},
                "user_id": {"type": "string", "description": "用户ID"},
            },
            "required": ["order_id", "user_id"],
        },
        category="refund",
        operation_type="read",
        risk_level="low",
        requires_confirmation=False,
        retryable=True,
    )
    async def refund_evaluate(order_id: str, user_id: str) -> dict:
        normalized_order_id = "" if order_id is None else str(order_id).strip()
        if not normalized_order_id:
            raise ValueError("order_id must not be empty")
        normalized_user_id = "" if user_id is None else str(user_id).strip()
        if not normalized_user_id:
            raise ValueError("user_id must not be empty")
        if refund_service is None:
            raise RuntimeError("refund service unavailable")
        return refund_service.evaluate(normalized_order_id, normalized_user_id).as_dict()

    @server.register(
        name="refund_create",
        description="创建订单退款申请",
        input_schema={
            "type": "object",
            "properties": {
                "order_id": {"type": "string", "description": "订单号"},
                "user_id": {"type": "string", "description": "用户ID"},
                "reason": {"type": "string", "description": "退款原因"},
            },
            "required": ["order_id", "user_id", "reason"],
        },
        category="refund",
        operation_type="write",
        risk_level="medium",
        requires_confirmation=True,
        retryable=False,
        recovery_fields=("order_id", "user_id"),
    )
    async def refund_create(order_id: str, user_id: str, reason: str) -> dict:
        normalized_order_id = "" if order_id is None else str(order_id).strip()
        if not normalized_order_id:
            raise ValueError("order_id must not be empty")
        normalized_user_id = "" if user_id is None else str(user_id).strip()
        if not normalized_user_id:
            raise ValueError("user_id must not be empty")
        normalized_reason = "" if reason is None else str(reason).strip()
        if not normalized_reason:
            raise ValueError("reason must not be empty")
        if refund_service is None:
            raise RuntimeError("refund service unavailable")
        return refund_service.create_refund(
            normalized_order_id,
            normalized_user_id,
            normalized_reason,
        ).as_dict()

    @server.register(
        name="knowledge_search",
        description="搜索企业知识库，返回相关文档片段",
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索查询"},
                "top_k": {"type": "integer", "description": "返回数量", "default": 3},
            },
            "required": ["query"],
        },
        category="knowledge",
    )
    async def knowledge_search(query: str, top_k: int = 3) -> list[dict]:
        query = str(query).strip()
        if not query:
            raise ValueError("query must not be empty")
        if long_term_memory:
            results = long_term_memory.search(query, top_k)
            return results
        return [
            {"content": f"关于'{query}'的知识库文档片段", "source": "FAQ.md", "score": 0.95},
        ]

    @server.register(
        name="ticket_create",
        description="创建客服工单",
        input_schema={
            "type": "object",
            "properties": {
                "client_request_id": {"type": "string"},
                "request_payload_hash": {"type": "string"},
                "user_id": {"type": "string"},
                "title": {"type": "string"},
                "description": {"type": "string"},
                "priority": {"type": "string", "enum": ["low", "medium", "high", "urgent"]},
                "category": {"type": "string"},
            },
            "required": [
                "client_request_id",
                "request_payload_hash",
                "user_id",
                "title",
                "description",
            ],
        },
        category="ticket",
        operation_type="write",
        risk_level="medium",
        requires_confirmation=True,
        retryable=False,
        recovery_fields=("client_request_id", "user_id", "request_payload_hash"),
    )
    async def ticket_create(
        title: str,
        description: str,
        priority: str = "medium",
        category: str = "general",
        client_request_id: str = "",
        request_payload_hash: str = "",
        user_id: str = "anonymous",
    ) -> dict:
        if ticket_service is None:
            return {"success": False, "reason_code": "ticket_service_unavailable"}
        if not client_request_id:
            return {"success": False, "reason_code": "invalid_client_request_id"}
        return ticket_service.create_ticket(
            client_request_id=client_request_id,
            user_id=user_id,
            title=title,
            description=description,
            priority=priority,
            ticket_type=category,
            request_payload_hash=request_payload_hash,
        )

    @server.register(
        name="ticket_query",
        description="按工单号查询本人客服工单",
        input_schema={
            "type": "object",
            "properties": {
                "ticket_id": {"type": "string"},
                "user_id": {"type": "string"},
            },
            "required": ["ticket_id", "user_id"],
        },
        category="ticket",
        operation_type="read",
        risk_level="low",
        requires_confirmation=False,
        retryable=True,
    )
    async def ticket_query(ticket_id: str, user_id: str = "anonymous") -> dict:
        if ticket_service is None:
            return {"success": False, "reason_code": "ticket_service_unavailable"}
        ticket = ticket_service.query_ticket(ticket_id, user_id)
        if ticket is None:
            return {
                "success": False,
                "reason_code": "ticket_not_found",
                "ticket_id": str(ticket_id or "").strip(),
            }
        visible_fields = (
            "ticket_id",
            "status",
            "category",
            "priority",
            "title",
            "created_at",
            "updated_at",
        )
        visible = {field: ticket[field] for field in visible_fields}
        visible.update(
            {
                "type": ticket.get("type", ticket.get("category", "general")),
                "ticket_type": ticket.get("ticket_type", ticket.get("category", "general")),
                "summary": ticket.get("summary", ticket.get("title", "")),
                "success": True,
                "reason_code": "found",
            }
        )
        return visible

    @server.register(
        name="risk_check",
        description="风控接口 — 检查交易/操作的风险等级",
        input_schema={
            "type": "object",
            "properties": {
                "user_id": {"type": "string"},
                "action": {"type": "string"},
                "amount": {"type": "number"},
            },
            "required": ["user_id", "action"],
        },
        category="compliance",
    )
    async def risk_check(user_id: str, action: str, amount: float = 0.0) -> dict:
        risk_level = "low"
        if amount > 50000:
            risk_level = "high"
        elif amount > 10000:
            risk_level = "medium"

        return {
            "user_id": user_id,
            "action": action,
            "risk_level": risk_level,
            "requires_manual_review": risk_level == "high",
        }

    return server
