from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field

import httpx
import pytest
from langchain_core.messages import AIMessage

from agents.orchestrator import create_chat_orchestrator
from auth.context import UserContext
from memory.long_term import HashEmbeddingBackend, LongTermMemory
from memory.session_store import SessionStore
from memory.short_term import ShortTermMemory
from rag.models import RetrievalHit


class _SmokeLLM:
    async def ainvoke(self, messages):
        system = "\n".join(str(getattr(message, "content", "")) for message in messages)
        human = "\n".join(
            str(getattr(message, "content", ""))
            for message in messages
            if getattr(message, "type", "") == "human"
        )
        if "意图识别Agent" in system:
            if "Loop Engineering" in human:
                payload = {
                    "primary_intent": "consultation",
                    "secondary_intent": "agent_engineering",
                    "confidence": 0.99,
                    "entities": {},
                    "suggested_agent": "knowledge_rag",
                }
            else:
                payload = {
                    "primary_intent": "conversation",
                    "secondary_intent": "capability_question",
                    "confidence": 0.99,
                    "entities": {},
                    "suggested_agent": "conversation",
                }
            return AIMessage(content=json.dumps(payload, ensure_ascii=False))
        if "向量检索" in human or "改写为更适合向量检索" in human:
            return AIMessage(content="Loop Engineering agent engineering")
        if "知识库问答Agent" in system:
            return AIMessage(
                content=(
                    "Loop Engineering 通过执行、验证和完成判定形成持续循环。"
                    "来源：agent_engineering/loop_engineering.md"
                )
            )
        if "金融合规审查Agent" in system:
            return AIMessage(
                content=json.dumps(
                    {"passed": True, "risk_level": "low", "violations": [], "suggestions": []},
                    ensure_ascii=False,
                )
            )
        return AIMessage(content="ok")


@dataclass
class _SmokeRetriever:
    calls: list[dict] = field(default_factory=list)

    def retrieve(self, query, *, domains=None, top_k=3, rerank=True):
        self.calls.append({"query": query, "domains": domains, "top_k": top_k, "rerank": rerank})
        return [
            RetrievalHit(
                chunk_id="agent-engineering-loop-1",
                domain="agent_engineering",
                rank=1,
                score=0.95,
                source="agent_engineering/loop_engineering.md",
                content="Loop Engineering 通过执行、验证和完成判定形成持续循环。",
            )
        ]

    def rerank(self, _query, candidates, top_k=3):
        return candidates[:top_k]


class _SmokeSessions:
    def __init__(self):
        self.rows: dict[str, dict] = {}

    async def create(self, account_id, title="", client_request_id=None, harness_version="legacy"):
        session_id = f"smoke-{uuid.uuid4().hex}"
        row = {
            "session_id": session_id,
            "account_id": account_id,
            "title": title,
            "client_request_id": client_request_id,
            # Phase 7: Sessions.create now pins the harness at creation; the
            # double follows the interface (values are not asserted here).
            "harness_version": harness_version,
        }
        self.rows[session_id] = row
        return row

    async def get_owned(self, session_id, account_id):
        row = self.rows.get(session_id)
        return row if row and row["account_id"] == account_id else None

    async def touch(self, session_id, account_id, title=""):
        row = await self.get_owned(session_id, account_id)
        if row is not None and title:
            row["title"] = title


def test_startup_rag_log_is_non_sensitive(monkeypatch, caplog):
    from api import main as api

    class _ConfiguredRetriever:
        is_artifact_mode = True
        sparse_mode = "global_corpus_v1"

        @staticmethod
        def _domains(_value):
            return ("apple_support", "agent_engineering")

    monkeypatch.setattr(api, "shared_retriever", _ConfiguredRetriever())
    monkeypatch.setenv("EMBEDDING_MODEL", "BAAI/bge-m3")
    monkeypatch.setenv("RAG_RERANKER_BACKEND", "cross_encoder")

    with caplog.at_level(logging.INFO, logger="api.main"):
        api._log_rag_runtime()

    assert (
        "RAG runtime: mode=artifact sparse_mode=global_corpus_v1 domains=apple_support,agent_engineering "
        "embedding=BAAI/bge-m3 reranker=cross_encoder"
    ) in caplog.text
    assert "OPENAI_API_KEY" not in caplog.text


@pytest.mark.asyncio
async def test_api_chat_rag_and_capability_smoke(monkeypatch):
    from api import main as api

    llm = _SmokeLLM()
    short_term = ShortTermMemory(redis_url="redis://127.0.0.1:6399/0", redis_retry_cooldown=60)
    session_store = SessionStore(short_term)
    retriever = _SmokeRetriever()
    long_term_memory = LongTermMemory(
        index_path="./.pytest-smartcs-rag-smoke-index",
        embedding_backend=HashEmbeddingBackend(),
    )
    orchestrator = create_chat_orchestrator(
        llm=llm,
        session_store=session_store,
        long_term_memory=long_term_memory,
        retriever=retriever,
    )
    sessions = _SmokeSessions()
    user = UserContext(account_id=7, username="smoke", business_user_id="user_001")

    monkeypatch.setattr(api, "chat_orchestrator", orchestrator)
    monkeypatch.setattr(api, "checkpoint_store", None)
    monkeypatch.setattr(api, "session_store", session_store)
    monkeypatch.setattr(api.app.state, "platform_sessions", sessions, raising=False)
    api.app.dependency_overrides[api.get_current_user] = lambda: user

    try:
        transport = httpx.ASGITransport(app=api.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            rag_response = await client.post("/api/chat", json={"message": "什么是 Loop Engineering？"})
            capability_response = await client.post("/api/chat", json={"message": "你不是双领域的 RAG 吗？"})

        assert rag_response.status_code == 200
        rag_payload = rag_response.json()
        assert rag_payload["intent"] == "knowledge_rag"
        assert "agent_engineering/loop_engineering.md" in rag_payload["response"]
        assert retriever.calls and retriever.calls[0]["query"] == "Loop Engineering agent engineering"
        assert retriever.calls[0]["domains"] == ()
        assert retriever.calls[0]["rerank"] is True

        assert capability_response.status_code == 200
        capability_payload = capability_response.json()
        assert capability_payload["intent"] == "conversation"
        assert "apple_support" in capability_payload["response"]
        assert "agent_engineering" in capability_payload["response"]
        assert "conversation 节点本轮不直接执行知识检索" in capability_payload["response"]
        assert len(retriever.calls) == 1
    finally:
        api.app.dependency_overrides.pop(api.get_current_user, None)
