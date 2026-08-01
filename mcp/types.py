"""
MCP 类型定义 —— 纯数据结构，没有任何行为逻辑。

理解方式：
    ToolDefinition  = 工具的"档案卡"（名字、说明、需要什么参数、谁来执行）
    ToolCallResult  = 调用后的"回执单"（成功没、结果是什么、花了多久）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Awaitable


@dataclass
class ToolDefinition:
    """注册一个工具时需要填写的内容。

    类比：食堂墙上的菜单条目。
    - name:          菜名，调用时按这个名字找
    - description:   说明，告诉 Agent 这个工具干什么用
    - input_schema:  参数的 JSON Schema，定义调用时能传什么参数
    - handler:       真正干活的 async 函数引用
    - category:      分组标签，方便分类管理
    - requires_auth: 是否需要鉴权
    """
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[..., Awaitable[Any]]
    category: str = "general"
    requires_auth: bool = False


@dataclass
class ToolCallResult:
    """一次工具调用之后返回的结果。

    类比：做完菜之后的"出餐单"。
    - tool_name:   调了哪个工具
    - success:     成功还是失败
    - result:      成功时的返回值
    - error:       失败时的错误信息
    - duration_ms: 耗时（毫秒），用来监控性能
    - timestamp:   调用时间
    """
    tool_name: str
    success: bool
    result: Any = None
    error: str | None = None
    duration_ms: float = 0.0
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
