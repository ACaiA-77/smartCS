"""
知识库搜索工具。
"""
from mcp.registry import ToolRegistry


def register_knowledge_tools(registry: ToolRegistry, retriever=None, long_term_memory=None) -> None:
    """向注册表注册所有知识库工具。"""

    shared_retriever = retriever
    if shared_retriever is None and long_term_memory is not None:
        shared_retriever = long_term_memory.get_retriever(use_env=False)

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
                "domain": {"type": "string", "description": "可选知识域"},
                "domains": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["query"],
        },
        category="knowledge",
    )
    async def knowledge_search(
        query: str,
        top_k: int = 3,
        domain: str | None = None,
        domains: list[str] | None = None,
    ) -> list[dict]:
        query = str(query).strip()
        if not query:
            raise ValueError("query must not be empty")
        if shared_retriever is None:
            return []
        selected_domains = domains or ([domain] if domain else None)
        results = shared_retriever.retrieve(
            query,
            domains=selected_domains,
            top_k=max(1, int(top_k)),
            rerank=True,
        )
        return [item.to_dict() if hasattr(item, "to_dict") else dict(item) for item in results]
