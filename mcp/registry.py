"""
MCP 工具注册表 —— 只管"工具字典"的增删查。

职责：把工具函数存起来、按名字找出来、列出所有工具。
不管：通信协议、具体工具实现。

类比：食堂墙上的菜单板。厨师把菜谱贴上去，点菜时按名字撕下来。
"""

from __future__ import annotations

from typing import Any, Callable, Awaitable

from mcp.types import ToolDefinition


class ToolRegistry:
    """工具注册表。核心就是一个字典：name → ToolDefinition。"""

    def __init__(self):
        self._tools: dict[str, ToolDefinition] = {}

    # ── 注册方式一：直接塞一个 ToolDefinition ──────────────────
    def register_tool(self, tool: ToolDefinition) -> None:
        """直接注册一个现成的 ToolDefinition 对象。"""
        self._tools[tool.name] = tool

    # ── 注册方式二：装饰器（最常用）──────────────────────────
    def register(
        self,
        name: str,
        description: str,
        input_schema: dict[str, Any],
        category: str = "general",
        requires_auth: bool = False,
    ) -> Callable:
        """装饰器：把被装饰的函数自动包装成 ToolDefinition 存起来。

        用法：
            @registry.register(name="xxx", description="...", input_schema={...})
            async def xxx(...):
                ...
        """
        def decorator(func: Callable[..., Awaitable[Any]]) -> Callable:
            tool = ToolDefinition(
                name=name,
                description=description,
                input_schema=input_schema,
                handler=func,          # ← 关键：保存函数引用
                category=category,
                requires_auth=requires_auth,
            )
            self._tools[name] = tool
            return func               # 返回原函数，不影响它被其他地方调用
        return decorator

    # ── 查找 ─────────────────────────────────────────────
    def get(self, name: str) -> ToolDefinition | None:
        """按名字查找工具。找不到返回 None。"""
        return self._tools.get(name)

    # ── 列出所有工具（供 Agent 发现）────────────────────────
    def list_tools(self, category: str | None = None) -> list[dict]:
        """返回所有已注册工具的元数据列表。

        Agent 连接上来先调这个，看看有哪些工具可用。
        注意不返回 handler，因为 handler 是函数，不能序列化发给远端。
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
            })
        return tools

    # ── 辅助 ─────────────────────────────────────────────
    def __contains__(self, name: str) -> bool:
        """支持 'xxx' in registry 的写法。"""
        return name in self._tools

    def __len__(self) -> int:
        """支持 len(registry) 的写法。"""
        return len(self._tools)
