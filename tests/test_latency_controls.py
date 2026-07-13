"""Regression tests for latency controls in the request path."""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

import memory.short_term as short_term_module
from agents.compliance_checker import ComplianceCheckerAgent
from agents.knowledge_rag import KnowledgeRAGAgent
from memory.short_term import ShortTermMemory


class FailingRedisClient:
    def __init__(self, attempts: list[int]):
        self.attempts = attempts

    async def ping(self):
        self.attempts.append(1)
        raise OSError("Redis is unavailable")


class FailingRedisModule:
    def __init__(self, attempts: list[int]):
        self.attempts = attempts

    def from_url(self, *args, **kwargs):
        return FailingRedisClient(self.attempts)


class CountingLLM:
    def __init__(self):
        self.call_count = 0

    async def ainvoke(self, messages):
        self.call_count += 1
        system = "".join(message.content for message in messages if isinstance(message, SystemMessage))
        human = "".join(message.content for message in messages if isinstance(message, HumanMessage))
        if "知识库问答 Agent" in system:
            return AIMessage(content="基于文档的回答")
        if "安全与隐私审查" in system:
            return AIMessage(content='{"passed": true, "risk_level": "low", "violations": [], "suggestions": []}')
        if "相关性排序专家" in system:
            return AIMessage(content="0")
        if "改写为更适合向量检索" in human:
            return AIMessage(content="iPhone 电池支持")
        return AIMessage(content="ok")


class ImmediateMemory:
    def search(self, query: str, top_k: int = 5):
        return [{"content": "iPhone 电池文档", "source": "apple.md", "score": 0.9, "metadata": {"doc_id": "d1"}}]


@pytest.mark.asyncio
async def test_unavailable_redis_is_not_pinged_for_every_short_term_operation(monkeypatch):
    attempts: list[int] = []
    monkeypatch.setattr(short_term_module, "aioredis", FailingRedisModule(attempts))
    memory = ShortTermMemory(redis_unavailable_retry_seconds=60)

    await memory.add_message("session-1", "user", "hello")
    await memory.get_history("session-1")
    await memory.add_message("session-1", "assistant", "world")

    assert len(attempts) == 1


@pytest.mark.asyncio
async def test_low_latency_rag_skips_optional_llm_rewrite_and_rerank():
    llm = CountingLLM()
    agent = KnowledgeRAGAgent(
        llm,
        ImmediateMemory(),
        enable_query_rewrite=False,
        enable_llm_rerank=False,
    )

    result = await agent.process(
        {
            "messages": [HumanMessage(content="iPhone 电池健康度在哪里查看？")],
            "sub_results": {"intent_router": {"secondary": "product_support", "entities": {"product": "iPhone"}}},
        }
    )

    assert result["sub_results"]["knowledge_rag"] == "基于文档的回答"
    assert llm.call_count == 1


@pytest.mark.asyncio
async def test_low_latency_compliance_keeps_rule_check_without_llm_review():
    llm = CountingLLM()
    checker = ComplianceCheckerAgent(llm, enable_llm_review=False)

    result = await checker.full_check("这是基于 Apple 支持文档的普通回答。")

    assert result.passed is True
    assert llm.call_count == 0


def test_default_settings_preserve_full_quality_path(monkeypatch):
    from api.settings import AppSettings

    for name in (
        "RAG_QUERY_REWRITE_ENABLED",
        "RAG_LLM_RERANK_ENABLED",
        "COMPLIANCE_LLM_REVIEW_ENABLED",
        "REDIS_UNAVAILABLE_RETRY_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)

    settings = AppSettings.from_env()

    assert settings.rag_query_rewrite_enabled is True
    assert settings.rag_llm_rerank_enabled is True
    assert settings.compliance_llm_review_enabled is True
    assert settings.redis_unavailable_retry_seconds > 0
