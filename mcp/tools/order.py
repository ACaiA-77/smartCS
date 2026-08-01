"""
订单相关工具。
"""
from mcp.registry import ToolRegistry


def register_order_tools(registry: ToolRegistry) -> None:
    """向注册表注册所有订单工具。"""

    @registry.register(
        name="order_query",
        description="查询订单信息，支持按订单号或用户ID查询",
        input_schema={
            "type": "object",
            "properties": {
                "order_id": {"type": "string", "description": "订单号"},
                "user_id": {"type": "string", "description": "用户ID"},
            },
        },
        category="order",
    )
    async def order_query(order_id: str = "", user_id: str = "") -> dict:
        return {
            "order_id": order_id or "ORD-20260401-001",
            "status": "shipped",
            "amount": 299.00,
            "product": "智能理财产品A",
            "created_at": "2026-04-01T10:00:00",
        }
