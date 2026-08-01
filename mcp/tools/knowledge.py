"""
知识库搜索工具。
"""
from mcp.registry import ToolRegistry


def register_knowledge_tools(registry: ToolRegistry) -> None:
    """向注册表注册所有知识库工具。"""

    @registry.register(
        name="knowledge_search",
        description="搜索企业知识库，返回相关文档片段",
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索查询"},
                "top_k": {
                    "type": "integer",
                    "description": "返回数量",
                    "default": 3,
                },
            },
            "required": ["query"],
        },
        category="knowledge",
    )
    async def knowledge_search(query: str, top_k: int = 3) -> list[dict]:
        return [
            {
                "content": f"关于'{query}'的知识库文档片段",
                "source": "FAQ.md",
                "score": 0.95,
            },
        ]
