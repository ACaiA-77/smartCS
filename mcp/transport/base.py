"""
MCP 传输层 —— 负责"用什么方式收发消息"。

标准 MCP 支持两种传输方式：
  - stdio：本地进程间通信（命令行工具直接启动 MCP Server）
  - HTTP：远程通信（网络上的 MCP Server）

当前实现：JSON-RPC 2.0 over HTTP（就是现在的 handle_jsonrpc 逻辑）。

类比：服务员手里的"点菜系统"——客人用什么方式下单、结果怎么送回。
不管菜怎么做（那是 registry 和 tools 的事）。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


# ── 抽象基类：定义传输层必须支持的操作 ─────────────────────

class BaseTransport(ABC):
    """传输层接口。所有传输方式（stdio、HTTP、WebSocket）都要实现它。"""

    @abstractmethod
    async def start(self) -> None:
        """启动传输层（比如监听端口）。"""
        ...

    @abstractmethod
    async def stop(self) -> None:
        """停止传输层。"""
        ...


# ── JSON-RPC 2.0 协议处理 ────────────────────────────────

class JsonRpcHandler:
    """JSON-RPC 2.0 协议处理器。

    职责：把收到的 JSON-RPC 请求拆包，分发给对应的处理函数，
         然后把结果重新打包成 JSON-RPC 响应格式返回。

    这跟你之前看到的 handle_jsonrpc 做的事情一模一样，
    只是现在独立成类了，不再跟工具注册表搅在一起。
    """

    # JSON-RPC 标准错误码
    METHOD_NOT_FOUND = -32601
    INTERNAL_ERROR = -32603

    def __init__(self, registry, call_log: list):
        """
        Args:
            registry:  ToolRegistry 实例（工具注册表）
            call_log:  调用日志列表（跟 MCPToolServer 共享）
        """
        self._registry = registry
        self._call_log = call_log

    async def handle(self, request: dict) -> dict:
        """处理一个 JSON-RPC 请求，返回 JSON-RPC 响应。

        这是传输层的核心入口。收到的请求长这样：
        {
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {"name": "order_query", "arguments": {...}},
            "id": 1
        }
        """
        import time

        method = request.get("method", "")
        params = request.get("params", {})
        req_id = request.get("id", 1)

        try:
            # ── 路由：根据 method 字段跳转 ──────────────────
            if method == "tools/list":
                result = self._registry.list_tools(
                    category=params.get("category")
                )

            elif method == "tools/call":
                tool_name = params.get("name", "")
                arguments = params.get("arguments", {})

                # 1. 查注册表
                tool = self._registry.get(tool_name)
                if tool is None:
                    result = {
                        "success": False,
                        "error": f"Tool '{tool_name}' not found. "
                                 f"Available: {list(self._registry._tools.keys())}",
                    }
                else:
                    # 2. 执行工具
                    start = time.time()
                    try:
                        output = await tool.handler(**arguments)
                        duration_ms = (time.time() - start) * 1000
                        result = {
                            "success": True,
                            "result": output,
                            "duration_ms": duration_ms,
                        }
                        # 3. 记调用日志
                        self._call_log.append({
                            "tool": tool_name,
                            "success": True,
                            "duration_ms": duration_ms,
                        })
                    except Exception as e:
                        duration_ms = (time.time() - start) * 1000
                        result = {
                            "success": False,
                            "error": str(e),
                            "duration_ms": duration_ms,
                        }
                        self._call_log.append({
                            "tool": tool_name,
                            "success": False,
                            "duration_ms": duration_ms,
                            "error": str(e),
                        })

            elif method == "ping":
                result = {"status": "ok"}

            else:
                return {
                    "jsonrpc": "2.0",
                    "error": {
                        "code": self.METHOD_NOT_FOUND,
                        "message": f"Method not found: {method}",
                    },
                    "id": req_id,
                }

            # ── 打包返回 ─────────────────────────────────
            return {"jsonrpc": "2.0", "result": result, "id": req_id}

        except Exception as e:
            return {
                "jsonrpc": "2.0",
                "error": {
                    "code": self.INTERNAL_ERROR,
                    "message": str(e),
                },
                "id": req_id,
            }
