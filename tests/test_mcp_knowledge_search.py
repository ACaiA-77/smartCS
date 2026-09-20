"""Regression tests for the FAISS-backed MCP knowledge_search tool."""

from __future__ import annotations

import pytest

from mcp.mcp_server import MCPToolServer, create_default_tools
from memory.long_term import LongTermMemory


@pytest.mark.asyncio
async def test_knowledge_search_returns_real_long_term_memory_documents() -> None:
    memory = LongTermMemory(embedding_dim=64)
    memory.add_document("Apple 账户忘记密码后可以通过 iforgot.apple.com 恢复。", "account.md")
    memory.add_document("App Store 购买项目可以在购买记录中申请退款。", "refund.md")
    server = create_default_tools(MCPToolServer(), long_term_memory=memory)

    result = await server.call_tool(
        "knowledge_search",
        {"query": "Apple 账户忘记密码怎么恢复", "top_k": 1},
    )

    assert result.success is True
    assert len(result.result) == 1
    document = result.result[0]
    assert document["content"] == "Apple 账户忘记密码后可以通过 iforgot.apple.com 恢复。"
    assert document["source"] == "account.md"
    assert isinstance(document["score"], float)
    assert document["metadata"]["doc_id"]


@pytest.mark.asyncio
async def test_explicit_memory_ignores_production_rag_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RAG_INDEX_ROOT", "./missing-production-index")
    memory = LongTermMemory(embedding_dim=64)
    memory.add_document("隔离知识库中的账户恢复说明。", "isolated.md")
    server = create_default_tools(MCPToolServer(), long_term_memory=memory)

    result = await server.call_tool(
        "knowledge_search",
        {"query": "账户恢复说明", "top_k": 1},
    )

    assert result.success is True
    assert result.result[0]["source"] == "isolated.md"


@pytest.mark.asyncio
async def test_knowledge_search_rejects_empty_query() -> None:
    server = create_default_tools(MCPToolServer(), long_term_memory=LongTermMemory(embedding_dim=64))

    result = await server.call_tool("knowledge_search", {"query": "   "})

    assert result.success is False
    assert result.error == "query must not be empty"
