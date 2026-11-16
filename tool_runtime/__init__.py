"""In-process tool runtime: tool definitions, execution policy and the ledger.

This package used to be named ``mcp``, which shadowed the official MCP SDK for
any process that had the repository root on ``sys.path``. It does not speak the
MCP wire protocol; it is the runtime that decides whether a tool call may
happen. The MCP gateway (``internal_api/mcp_gateway.py``) is the only place the
official SDK is used, and it can now import it normally.
"""

from tool_runtime.mcp_server import MCPToolServer, create_default_tools

__all__ = ["MCPToolServer", "create_default_tools"]
