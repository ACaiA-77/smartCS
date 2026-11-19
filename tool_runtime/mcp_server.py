"""
MCP工具协议服务端 — Model Context Protocol实现
遵循Anthropic MCP标准，通过JSON-RPC 2.0提供工具注册/发现/调用能力。
支持动态工具扩展，Agent通过统一接口调用外部系统。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Awaitable, Callable


@dataclass(frozen=True)
class LocalAtomicWrite:
    """Trusted synchronous SQLite entry point, bound to its registered handler."""

    repository: Any
    handler: Callable[..., Any]
    execute: Callable[[sqlite3.Connection, dict[str, Any]], Any]


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
    local_atomic_write: LocalAtomicWrite | None = None


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
    3. 工具调用 (Tool Invocation) - 通过JSON-RPC 2.0协议调用
    4. 结果返回 (Result Delivery)
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
        """工具发现：列出所有可用工具（对应 MCP tools/list）。"""
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
        """工具调用：执行指定工具（对应 MCP tools/call）。"""
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
        """处理JSON-RPC 2.0请求。"""
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
    retriever=None,
    vault=None,
    ontology=None,
    wiki_compiler=None,
    mail_router=None,
    github_client=None,
    feishu_client=None,
) -> MCPToolServer:
    """注册 Personal Copilot 默认工具集（共享知识检索 + 已配置连接器工具）。"""

    @server.register(
        name="knowledge_search",
        description="搜索知识库，返回相关文档片段",
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索查询"},
                "top_k": {"type": "integer", "description": "返回数量", "default": 3},
                "domain": {"type": "string", "description": "可选知识域"},
                "domains": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["query"],
        },
        category="knowledge",
    )
    async def knowledge_search(
        query: str,
        top_k: int = 3,
        domain: str | None = None,
        domains: list[str] | None = None,
    ) -> list[dict]:
        query = str(query).strip()
        if not query:
            raise ValueError("query must not be empty")
        shared_retriever = retriever
        if shared_retriever is None and long_term_memory is not None:
            shared_retriever = long_term_memory.get_retriever(use_env=False)
        if shared_retriever is None:
            return []
        selected_domains = domains or ([domain] if domain else None)
        results = shared_retriever.retrieve(
            query,
            domains=selected_domains,
            top_k=max(1, int(top_k)),
            rerank=True,
        )
        return [item.to_dict() if hasattr(item, "to_dict") else dict(item) for item in results]

    if any(
        dependency is not None
        for dependency in (
            vault,
            ontology,
            wiki_compiler,
            mail_router,
            github_client,
            feishu_client,
        )
    ):
        from tool_runtime.copilot_tools import register_copilot_tools

        register_copilot_tools(
            server,
            vault=vault,
            ontology=ontology,
            wiki_compiler=wiki_compiler,
            mail_router=mail_router,
            github_client=github_client,
            feishu_client=feishu_client,
        )

    return server
