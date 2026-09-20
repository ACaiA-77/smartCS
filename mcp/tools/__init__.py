from mcp.registry import ToolRegistry
from mcp.tools.order import register_order_tools
from mcp.tools.knowledge import register_knowledge_tools
from mcp.tools.ticket import register_ticket_tools
from mcp.tools.compliance import register_compliance_tools

__all__ = ["register_all_tools"]


def register_all_tools(registry: ToolRegistry, retriever=None, long_term_memory=None) -> ToolRegistry:
    """向注册表注册所有默认工具。

    每个工具模块只暴露一个 register_xxx_tools 函数，
    需要新增工具时：
      1. 在 tools/ 下新建一个 .py 文件
      2. 写一个 register_xxx_tools(registry) 函数
      3. 在这里加一行调用
    """
    register_order_tools(registry)
    register_knowledge_tools(
        registry, retriever=retriever, long_term_memory=long_term_memory
    )
    register_ticket_tools(registry)
    register_compliance_tools(registry)
    return registry
