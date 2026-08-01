"""Regression tests for the SQLite-backed MCP order_query tool."""

from __future__ import annotations

import pytest

from mcp.mcp_server import MCPToolServer, create_default_tools
from mcp.order_repository import OrderRepository
from memory.long_term import LongTermMemory


def _server(repository: OrderRepository) -> MCPToolServer:
    return create_default_tools(
        MCPToolServer(),
        long_term_memory=LongTermMemory(embedding_dim=64),
        order_repository=repository,
    )


@pytest.mark.asyncio
async def test_order_query_reads_seeded_local_domestic_order(tmp_path) -> None:
    repository = OrderRepository(str(tmp_path / "orders.db"))

    result = await _server(repository).call_tool(
        "order_query",
        {"order_id": "ORD-20260801-0001", "user_id": "user_001"},
    )

    assert result.success is True
    assert result.result["found"] is True
    assert result.result["data_source"] == "SQLite 本地国内电商演示数据"
    assert result.result["order_id"] == "ORD-20260801-0001"
    assert result.result["status_label"] == "待付款"
    assert result.result["products"]
    assert result.result["recipient_phone_masked"].count("*") == 4


@pytest.mark.asyncio
async def test_order_query_returns_not_found_for_missing_local_demo_order(tmp_path) -> None:
    repository = OrderRepository(str(tmp_path / "orders.db"))

    result = await _server(repository).call_tool("order_query", {"order_id": "ORD-20260801-9999"})

    assert result.success is True
    assert result.result == {
        "found": False,
        "order_id": "ORD-20260801-9999",
        "data_source": "SQLite 本地国内电商演示数据",
        "message": "本地演示订单不存在",
    }


@pytest.mark.asyncio
async def test_order_query_rejects_empty_order_id(tmp_path) -> None:
    result = await _server(OrderRepository(str(tmp_path / "orders.db"))).call_tool("order_query", {"order_id": ""})

    assert result.success is False
    assert result.error == "order_id must not be empty"


def test_order_repository_seeds_exactly_one_hundred_orders(tmp_path) -> None:
    repository = OrderRepository(str(tmp_path / "orders.db"))

    assert repository.count_orders() == 100
    assert len(repository.list_orders(limit=20)) == 20
