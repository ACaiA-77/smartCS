"""
MCP Server 编排器 —— 把注册表和传输层串起来。

这是整个 MCP 模块的"总控"。
原来的 mcp_server.py 里 MCPToolServer 同时干了 3 件事：
  1. 管理工具注册表（现在拆到 registry.py）
  2. 处理 JSON-RPC（现在拆到 transport/base.py）
  3. 记调用日志（保留在这里，因为日志属于"服务器状态"）

现在 MCPToolServer 只干一件事：组装部件 + 暴露统一入口。
"""

from __future__ import annotations

from mcp.registry import ToolRegistry
from mcp.transport.base import JsonRpcHandler


class MCPToolServer:
    """MCP 工具服务器。

    组装关系：
        MCPToolServer
          ├── ToolRegistry   ← 工具字典
          ├── JsonRpcHandler ← 协议处理（把 JSON-RPC 请求翻译成工具调用）
          └── _call_log      ← 调用审计日志
    """

    def __init__(self):
        self.registry = ToolRegistry()                  # 工具注册表
        self._call_log: list[dict] = []                 # 调用历史
        self._jsonrpc = JsonRpcHandler(                 # 协议处理器
            self.registry,
            self._call_log,
        )

    # ── 代理方法：把注册操作直接委托给 ToolRegistry ──────────

    def register_tool(self, tool):
        """注册工具（直接传 ToolDefinition）。"""
        return self.registry.register_tool(tool)

    def register(self, name, description, input_schema,
                 category="general", requires_auth=False):
        """装饰器方式注册工具。

        用法不变：
            @server.register(name="xxx", description="...", input_schema={...})
            async def xxx(...): ...
        """
        return self.registry.register(
            name, description, input_schema, category, requires_auth
        )

    def list_tools(self, category=None):
        """列出所有工具。"""
        return self.registry.list_tools(category)

    # ── 协议入口：收到 JSON-RPC 请求，交给 JsonRpcHandler 处理 ──

    async def handle_jsonrpc(self, request: dict) -> dict:
        """处理一个 JSON-RPC 请求。

        这是一个纯转发方法：收到请求 → 交给 JsonRpcHandler → 返回响应。
        如果你以后想换传输方式（比如从 HTTP 换成 WebSocket），
        只需要改这个方法怎么被调用的，里面的逻辑不用动。
        """
        return await self._jsonrpc.handle(request)

    # ── 运维接口 ────────────────────────────────────────────

    def get_call_log(self, last_n: int = 100) -> list[dict]:
        """获取最近的工具调用日志。"""
        return self._call_log[-last_n:]
