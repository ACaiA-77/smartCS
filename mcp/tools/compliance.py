"""
风控/合规工具。
"""
from mcp.registry import ToolRegistry


def register_compliance_tools(registry: ToolRegistry) -> None:
    """向注册表注册所有合规工具。"""

    @registry.register(
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
    async def risk_check(
        user_id: str,
        action: str,
        amount: float = 0.0,
    ) -> dict:
        if amount > 50000:
            risk_level = "high"
        elif amount > 10000:
            risk_level = "medium"
        else:
            risk_level = "low"

        return {
            "user_id": user_id,
            "action": action,
            "risk_level": risk_level,
            "requires_manual_review": risk_level == "high",
        }
