"""
工单工具。
"""
import uuid

from mcp.registry import ToolRegistry


def register_ticket_tools(registry: ToolRegistry) -> None:
    """向注册表注册所有工单工具。"""

    @registry.register(
        name="ticket_create",
        description="创建客服工单",
        input_schema={
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "description": {"type": "string"},
                "priority": {
                    "type": "string",
                    "enum": ["low", "medium", "high", "urgent"],
                },
                "category": {"type": "string"},
            },
            "required": ["title", "description"],
        },
        category="ticket",
    )
    async def ticket_create(
        title: str,
        description: str,
        priority: str = "medium",
        category: str = "general",
    ) -> dict:
        return {
            "ticket_id": f"TK-{uuid.uuid4().hex[:8].upper()}",
            "title": title,
            "status": "created",
            "priority": priority,
        }
