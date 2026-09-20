"""
MCP 模块 —— Model Context Protocol 的 Python 实现。

架构（按标准 MCP 拆成 5 层）：

    mcp/
    ├── types.py          # 纯数据结构（ToolDefinition、ToolCallResult）
    ├── registry.py       # 工具注册表（存、取、列）
    ├── transport/
    │   └── base.py       # 传输层（JSON-RPC 2.0 协议处理）
    ├── server.py         # 编排器（把 registry + transport 串起来）
    └── tools/            # 具体工具实现
        ├── order.py
        ├── knowledge.py
        ├── ticket.py
        └── compliance.py

用法（一行创建，三行添加工具，一行收到请求）:

    server = create_server()
    server.handle_jsonrpc({...})
"""

from mcp.server import MCPToolServer
from mcp.tools import register_all_tools

__all__ = ["MCPToolServer", "create_server"]


def create_server(retriever=None, long_term_memory=None) -> MCPToolServer:
    """工厂函数：创建 MCPToolServer 并注册所有默认工具。

    等价于原来的 create_default_tools()，
    但工具注册逻辑拆到了 tools/ 目录下。
    """
    server = MCPToolServer()
    register_all_tools(
        server.registry,
        retriever=retriever,
        long_term_memory=long_term_memory,
    )
    return server
