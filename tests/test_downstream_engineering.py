"""Regression tests for Apple-domain downstream engineering behavior."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agents.compliance_checker import COMPLIANCE_SYSTEM_PROMPT, ComplianceCheckerAgent
from agents.knowledge_rag import RAG_SYSTEM_PROMPT
from agents.ticket_handler import TicketHandlerAgent, TicketType
from mcp.mcp_server import MCPToolServer, create_default_tools
from tests.conftest import MockLLM


class FailingTicketMCP:
    async def call_tool(self, name: str, arguments: dict):
        return SimpleNamespace(success=False, result=None, error="upstream unavailable")


def test_downstream_prompts_no_longer_contain_financial_domain_policy():
    assert "金融" not in RAG_SYSTEM_PROMPT
    assert "金融" not in COMPLIANCE_SYSTEM_PROMPT
    assert "理赔" not in COMPLIANCE_SYSTEM_PROMPT


@pytest.mark.asyncio
async def test_ticket_create_surfaces_mcp_failure_instead_of_claiming_success():
    agent = TicketHandlerAgent(MockLLM(), mcp_server=FailingTicketMCP())

    result = await agent.create_ticket(
        {
            "ticket_type": TicketType.REPAIR.value,
            "priority": "medium",
            "summary": "iPhone 屏幕维修",
            "details": "屏幕碎裂",
        },
        user_id="u-1",
    )

    assert "未能创建" in result
    assert "工单已创建成功" not in result


def test_short_email_is_masked_without_leaking_original_value():
    agent = ComplianceCheckerAgent(MockLLM())

    assert agent._mask_pii("请联系 a@b.co") != "请联系 a@b.co"
    assert "*" in agent._mask_pii("请联系 a@b.co")


@pytest.mark.asyncio
async def test_compliance_audit_contains_decision_but_not_raw_pii():
    agent = ComplianceCheckerAgent(MockLLM())
    raw = "请联系 a@b.co"

    result = await agent.full_check(raw)

    assert result.passed is False
    assert agent.decision_audit[-1]["risk_level"] == "high"
    assert raw not in str(agent.decision_audit[-1])


@pytest.mark.asyncio
async def test_default_mcp_order_mock_uses_apple_domain_data():
    server = create_default_tools(MCPToolServer())

    result = await server.call_tool("order_query", {"order_id": "ORD-APPLE-1", "user_id": "u-1"})

    assert result.success is True
    assert "Apple" in result.result["product"]
