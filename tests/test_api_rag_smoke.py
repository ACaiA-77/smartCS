from __future__ import annotations

import logging
import uuid

import httpx
import pytest

from auth.context import UserContext
from tests.app_state import patch_app_state


class _SmokeSessions:
    def __init__(self):
        self.rows: dict[str, dict] = {}

    async def create(self, account_id, title="", client_request_id=None, harness_version="pi"):
        session_id = f"smoke-{uuid.uuid4().hex}"
        row = {
            "session_id": session_id,
            "account_id": account_id,
            "title": title,
            "client_request_id": client_request_id,
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
async def test_api_chat_stream_forwards_sse_frames(monkeypatch):
    from api import main as api

    sessions = _SmokeSessions()
    user = UserContext(account_id=7, username="smoke", business_user_id="user_001")

    async def fake_stream_chat(**kwargs):
        yield b'event: status\ndata: {"type":"status","text":"\xe6\xad\xa3\xe5\x9c\xa8\xe6\xa3\x80\xe7\xb4\xa2\xe6\x9c\x8d\xe5\x8a\xa1\xe6\x94\xbf\xe7\xad\x96","at":1}\n\n'
        yield b'event: final\ndata: {"type":"final","text":"Loop Engineering \xe5\x9b\x9e\xe7\xad\x94","at":2}\n\n'
        yield f'event: meta\ndata: {{"session_id":"{kwargs["session_id"]}","client_request_id":"{kwargs["client_request_id"]}","replayed":false}}\n\n'.encode("utf-8")

    monkeypatch.setattr(api, "stream_chat", fake_stream_chat)
    patch_app_state(monkeypatch, api.app, platform_sessions=sessions)
    api.app.dependency_overrides[api.get_current_user] = lambda: user
    try:
        transport = httpx.ASGITransport(app=api.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post("/api/chat/stream", json={"message": "什么是 Loop Engineering？"})
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert "event: status" in response.text
        assert "event: final" in response.text
        assert "Loop Engineering 回答" in response.text
    finally:
        api.app.dependency_overrides.pop(api.get_current_user, None)
