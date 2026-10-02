"""Deterministic cross-component tests for centralized context integration."""

from __future__ import annotations

import asyncio
import hashlib
import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from agents.compliance_checker import COMPLIANCE_SYSTEM_PROMPT, ComplianceCheckerAgent
from agents.conversation import CONVERSATION_SYSTEM_PROMPT, ConversationAgent
from agents.intent_router import INTENT_SYSTEM_PROMPT, IntentRouterAgent
from agents.knowledge_rag import QUERY_REWRITE_PROMPT, RAG_SYSTEM_PROMPT, KnowledgeRAGAgent
from agents.orchestrator import ChatOrchestrator
from agents.ticket_handler import TICKET_SYSTEM_PROMPT, TicketHandlerAgent
from checkpoint.models import AgentCheckpoint, CheckpointConflict, CheckpointOwnershipError
from context.invocation import configure_context_manager
from context.manager import ContextManager, active_context
from context.models import ContextOverflowError, ModelProfile
from memory.long_term import LongTermMemory
from memory.session_store import ConversationState, SessionStore
from memory.user_memory import InMemoryUserMemoryRepository, UserMemoryService
from memory.short_term import ShortTermMemory
from mcp.execution_ledger import ExecutionLedger
from mcp.mcp_server import MCPToolServer, ToolDefinition
from mcp.tool_execution import ToolExecutionContext, ToolExecutor
from tests.conftest import MockLLM


PROFILE = ModelProfile(
    name="unit-context-model",
    provider="test",
    context_limit=8192,
    max_output_tokens=512,
    reserve=128,
    safety_margin=128,
    soft_ratio=0.8,
    hard_ratio=0.9,
    recent_messages=4,
    compression_attempts=4,
)


class CharacterTokenizer:
    def encode(self, text: str) -> list[int]:
        return list(range(len(text)))


class RecordingLLM:
    """Injected deterministic model; it does not represent online quality."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def ainvoke(self, messages, **kwargs):
        self.calls.append({"messages": list(messages), "kwargs": dict(kwargs)})
        system = "\n".join(
            str(message.content)
            for message in messages
            if isinstance(message, SystemMessage)
        )
        human = "\n".join(
            str(message.content)
            for message in messages
            if isinstance(message, HumanMessage)
        )
        if "意图识别Agent" in system:
            content = json.dumps({
                "primary_intent": "consultation",
                "secondary_intent": "product_inquiry",
                "confidence": 0.9,
                "entities": {},
                "suggested_agent": "knowledge_rag",
            }, ensure_ascii=False)
        elif "工单处理Agent" in system:
            content = json.dumps({
                "action": "query", "ticket_type": "general", "priority": "low",
                "summary": "测试", "details": "测试",
            }, ensure_ascii=False)
        elif "合规审查Agent" in system or "金融合规审查" in system:
            content = json.dumps({
                "passed": True, "risk_level": "low", "violations": [], "suggestions": [],
            }, ensure_ascii=False)
        elif "知识库问答Agent" in system:
            content = "基于注入证据生成的测试回答。"
        elif "向量检索的查询语句" in human:
            content = "改写后的检索查询"
        else:
            content = "注入模型的自然对话回答。"
        return AIMessage(content=content)


class ScopeObservingLLM(RecordingLLM):
    """Injected model which records the request scope visible at invocation."""

    def __init__(self) -> None:
        super().__init__()
        self.observations: list[dict] = []

    async def ainvoke(self, messages, **kwargs):
        scope = active_context.get()
        observation = {
            "scope": scope,
            "owner": (scope.session_id, scope.user_id) if scope is not None else None,
            "request_id": scope.request_id if scope is not None else None,
            "execution_id": scope.execution_id if scope is not None else None,
            "event_counter": scope.event_counter if scope is not None else None,
            "state": scope.state if scope is not None else None,
            "package": scope.last_context_package if scope is not None else None,
            "messages": list(messages),
        }
        self.observations.append(observation)
        await asyncio.sleep(0)
        assert active_context.get() is scope
        return await super().ainvoke(messages, **kwargs)


class MemoryWorkingSetCache:
    """Owner-keyed cache double used to exercise checkpoint synchronization."""

    def __init__(self) -> None:
        self.values: dict[tuple[str, str], dict] = {}

    @property
    def backend_status(self) -> dict[str, object]:
        return {"backend": "test", "redis_available": False}

    async def get(self, session_id: str, user_id: str) -> dict | None:
        value = self.values.get((session_id, user_id))
        return deepcopy(value) if value is not None else None

    async def put(self, session_id: str, user_id: str, working_set: dict) -> None:
        self.values[(session_id, user_id)] = deepcopy(working_set)

    async def invalidate(self, session_id: str, user_id: str) -> None:
        self.values.pop((session_id, user_id), None)


class QueryContextReadSpy:
    """Fails loudly if a query-only invocation hydrates conversation storage."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def load_working_set(self, *_args, **_kwargs):
        self.calls.append("load_working_set")
        raise AssertionError("query-only policy must not read session storage")

    async def recent_events(self, *_args, **_kwargs):
        self.calls.append("recent_events")
        raise AssertionError("query-only policy must not read session events")

    async def events_range(self, *_args, **_kwargs):
        self.calls.append("events_range")
        raise AssertionError("query-only policy must not read session events")


class MemoryEventStore:
    """Small owner-scoped event-store double matching the production interface."""

    def __init__(self) -> None:
        self.events: dict[tuple[str, str], list[dict]] = {}
        self.digests: dict[tuple[str, str], dict] = {}
        self.checkpoints: dict[tuple[str, str], AgentCheckpoint] = {}
        self.receipts: dict[tuple[str, str], tuple[str, dict]] = {}
        self.locks: dict[str, asyncio.Lock] = {}
        self.history_calls = 0
        self.source_repository = None

    def _events(self, session_id: str, user_id: str) -> list[dict]:
        key = (session_id, user_id)
        for (sid, owner), events in self.events.items():
            if sid == session_id and owner != user_id and events:
                raise CheckpointOwnershipError("session belongs to another user")
        return self.events.setdefault(key, [])

    @asynccontextmanager
    async def session_lock(self, session_id: str):
        lock = self.locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            yield

    async def append_event(self, session_id, user_id, event_type, payload, *, event_key=None):
        events = self._events(session_id, user_id)
        if event_key:
            for event in events:
                if event.get("event_key") == event_key:
                    if event["event_type"] != event_type or event["payload"] != payload:
                        raise CheckpointConflict("event key reused with different content")
                    return deepcopy(event)
        event = {
            "event_id": len(events) + 1,
            "seq": len(events) + 1,
            "event_type": event_type,
            "payload": deepcopy(payload),
            "event_key": event_key,
            "created_at": "2026-01-01T00:00:00+00:00",
        }
        events.append(event)
        if (
            self.source_repository is not None
            and event_type == "USER_MESSAGE"
            and not payload.get("synthetic")
        ):
            self.source_repository.add_source(
                user_id,
                session_id,
                str(event["event_id"]),
                str(payload.get("content", "")),
                seq=event["seq"],
                created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                event_type=event_type,
            )
        return deepcopy(event)

    async def recent_events(self, session_id, user_id, *, after_seq=0, limit=100):
        events = self._events(session_id, user_id)
        return deepcopy([event for event in events if event["seq"] > after_seq][:limit])

    @staticmethod
    def _message_history(events: list[dict]) -> list[dict[str, str]]:
        history = []
        for event in events:
            if event["event_type"] not in {"USER_MESSAGE", "ASSISTANT_MESSAGE", "MESSAGE"}:
                continue
            payload = event["payload"]
            role = payload.get("role") or ("user" if event["event_type"] == "USER_MESSAGE" else "assistant")
            if role in {"user", "assistant"} and isinstance(payload.get("content"), str):
                history.append({"role": role, "content": payload["content"]})
        return history

    async def history(self, session_id, user_id):
        self.history_calls += 1
        return self._message_history(self._events(session_id, user_id))

    async def load_working_set(self, session_id, user_id, *, recent_limit=8):
        events = self._events(session_id, user_id)
        messages = self._message_history(events)
        checkpoint = self.checkpoints.get((session_id, user_id))
        digest = self.digests.get((session_id, user_id), {})
        return {
            "session_id": session_id,
            "user_id": user_id,
            "recent_messages": messages[-recent_limit:] if recent_limit else [],
            "recent_tool_events": [],
            "rolling_summary": digest.get("rolling_summary"),
            "archive_summary": deepcopy(digest.get("archive_summary")),
            "session_state": deepcopy(checkpoint.context.get("session_state", {})) if checkpoint else {},
            "last_event_seq": len(events),
            "version": digest.get("version", 0),
            "checkpoint_version": checkpoint.version if checkpoint else 0,
            "protected_fields": deepcopy(digest.get("protected_fields", {})),
            "summary_event_seq": digest.get("summary_event_seq", 0),
        }

    async def save_digest(self, session_id, user_id, *, rolling_summary=None, archive_summary=None,
                          protected_fields=None, summary_event_seq=None):
        digest = self.digests.setdefault((session_id, user_id), {"version": 0, "summary_event_seq": 0})
        if rolling_summary is not None:
            digest["rolling_summary"] = rolling_summary
        if archive_summary is not None:
            digest["archive_summary"] = deepcopy(archive_summary)
        if protected_fields is not None:
            digest["protected_fields"] = deepcopy(protected_fields)
        if summary_event_seq is not None:
            digest["summary_event_seq"] = summary_event_seq
        digest["version"] += 1

    async def load(self, session_id, user_id):
        events = self._events(session_id, user_id)
        value = self.checkpoints.get((session_id, user_id))
        if value is None:
            return None
        return AgentCheckpoint.model_validate({
            **value.model_dump(),
            "messages": self._message_history(events)[-20:],
        })

    async def _sync_checkpoint_events(self, checkpoint):
        request_id = checkpoint.context.get("request_id")
        if not request_id:
            return
        for message in checkpoint.messages:
            role = message.role
            content = message.content
            event_type = "USER_MESSAGE" if role == "user" else "ASSISTANT_MESSAGE"
            digest = hashlib.sha256(content.encode()).hexdigest()[:16]
            await self.append_event(
                checkpoint.session_id,
                checkpoint.user_id,
                event_type,
                {"role": role, "content": content},
                event_key=f"checkpoint:{request_id}:message:{role}:{digest}",
            )
        state_payload = {
            "request_id": request_id,
            "stage": checkpoint.current_stage,
            "status": checkpoint.status,
            "intent": checkpoint.intent,
            "session_state": checkpoint.context.get("session_state", {}),
        }
        await self.append_event(
            checkpoint.session_id,
            checkpoint.user_id,
            "STATE_CHANGE",
            state_payload,
            event_key=f"checkpoint:{request_id}:stage:{checkpoint.current_stage}",
        )

    async def save(self, checkpoint):
        key = (checkpoint.session_id, checkpoint.user_id)
        self._events(*key)
        await self._sync_checkpoint_events(checkpoint)
        value = checkpoint.model_copy(update={"version": 1, "last_event_seq": len(self.events[key])}, deep=True)
        self.checkpoints[key] = value
        return value.model_copy(deep=True)

    async def update(self, checkpoint):
        key = (checkpoint.session_id, checkpoint.user_id)
        self._events(*key)
        current = self.checkpoints.get(key)
        if current is None or current.version != checkpoint.version:
            raise CheckpointConflict("checkpoint version changed")
        await self._sync_checkpoint_events(checkpoint)
        value = AgentCheckpoint.model_validate({
            **checkpoint.model_dump(),
            "version": checkpoint.version + 1,
            "last_event_seq": len(self.events[key]),
        })
        self.checkpoints[key] = value
        if value.status in {"finished", "waiting"} and value.context.get("request_id"):
            response = {
                "final_response": value.context["state"]["final_response"],
                "intent": value.intent,
                "compliance_passed": value.context["state"]["compliance_passed"],
                "client_request_id": value.context["request_id"],
                "session_id": value.session_id,
            }
            self.receipts[(value.session_id, value.context["request_id"])] = (
                value.context["request_hash"], response,
            )
        return value.model_copy(deep=True)

    async def receipt(self, session_id, user_id, request_id, request_hash):
        value = self.receipts.get((session_id, request_id))
        if value is None:
            return None
        stored_hash, response = value
        if stored_hash != request_hash:
            raise CheckpointConflict("request id reused with different content")
        return deepcopy(response)


class MemorySpy:
    def __init__(self) -> None:
        self.process_message = AsyncMock()
        self.drain_pending = AsyncMock()


def _messages(call: dict, message_type):
    return [message for message in call["messages"] if isinstance(message, message_type)]


@pytest.mark.asyncio
async def test_all_agent_llm_calls_use_central_budget_and_reassemble_each_call():
    manager = ContextManager(tokenizer=CharacterTokenizer(), default_model=PROFILE)
    configure_context_manager(manager)
    llm = RecordingLLM()
    state = {
        "session_id": "unit-session",
        "user_id": "unit-user",
        "messages": [
            HumanMessage(content="早先问题"),
            AIMessage(content="早先回答"),
            HumanMessage(content="本轮上下文主题"),
            HumanMessage(content="当前问题"),
        ],
        "intent": "knowledge_rag",
        "sub_results": {},
    }
    try:
        await IntentRouterAgent(llm).classify("当前问题", state=state)
        await ConversationAgent(llm).process(state)
        rag = KnowledgeRAGAgent(llm, LongTermMemory())
        rewritten = await rag.rewrite_query("当前检索问题")
        answer = await rag.generate_answer(
            "当前RAG问题",
            [{"source": "policy.md", "content": "仅此段落作为检索证据"}],
            state=state,
        )
        ticket = await TicketHandlerAgent(llm).analyze_request("请查询工单", state=state)
        compliance = await ComplianceCheckerAgent(llm).llm_check("安全且简洁的客服回答")
        changed_state = {
            **state,
            "messages": [HumanMessage(content="完全不同的新问题")],
        }
        await ConversationAgent(llm).process(changed_state)

        assert rewritten == "改写后的检索查询"
        assert answer == "基于注入证据生成的测试回答。"
        assert ticket["action"] == "query"
        assert compliance.passed is True
        assert len(llm.calls) == 7
        assert manager.metrics_snapshot()["context_invocations_total"] == 7
        assert manager.metrics_snapshot()["context_builds_total"] == 7

        conversation_system = _messages(llm.calls[1], SystemMessage)[0].content
        assert conversation_system.startswith(f"<System>\n{CONVERSATION_SYSTEM_PROMPT}")
        rag_system = _messages(llm.calls[3], SystemMessage)[0].content
        assert rag_system.startswith(f"<System>\n{RAG_SYSTEM_PROMPT}")
        assert "policy.md" not in _messages(llm.calls[3], HumanMessage)[0].content.split("<Evidence>")[0]
        assert "仅此段落作为检索证据" in _messages(llm.calls[3], HumanMessage)[0].content
        rewrite_human = _messages(llm.calls[2], HumanMessage)[0].content
        assert "当前检索问题" in rewrite_human
        assert "早先问题" not in rewrite_human
        assert "工单处理Agent" in _messages(llm.calls[4], SystemMessage)[0].content
        assert "金融合规审查Agent" in _messages(llm.calls[5], SystemMessage)[0].content
        assert all(call["kwargs"].get("max_tokens") == PROFILE.max_output_tokens for call in llm.calls)
        assert all("<CurrentUser>" in _messages(call, HumanMessage)[0].content for call in llm.calls)
        assert "当前问题" in _messages(llm.calls[1], HumanMessage)[0].content
        assert "完全不同的新问题" in _messages(llm.calls[6], HumanMessage)[0].content
        assert _messages(llm.calls[1], HumanMessage)[0].content != _messages(llm.calls[6], HumanMessage)[0].content
        assert active_context.get() is None
    finally:
        configure_context_manager(None)


@pytest.mark.asyncio
async def test_isolated_query_only_calls_reuse_request_scope_without_reading_history_or_memory():
    storage = QueryContextReadSpy()
    user_memory = SimpleNamespace(
        profile_cards=AsyncMock(return_value=[{"private": "profile"}]),
        retrieve=AsyncMock(return_value=[{"private": "retrieved memory"}]),
    )
    manager = ContextManager(
        event_store=storage,
        user_memory=user_memory,
        tokenizer=CharacterTokenizer(),
        default_model=PROFILE,
    )
    llm = ScopeObservingLLM()
    rag = KnowledgeRAGAgent(llm, LongTermMemory())
    checker = ComplianceCheckerAgent(llm)
    state = {
        "session_id": "isolated-request-session",
        "user_id": "isolated-request-owner",
        "messages": [HumanMessage(content="PRIVATE prior conversation")],
        "private_state": "PRIVATE workflow state",
    }

    async with manager.bind_request(
        state["session_id"], state["user_id"], request_id="isolated-request-id", state=state
    ) as request_scope:
        request_scope.event_counter = 23
        execution_id = request_scope.execution_id

        original_query = "请查找原始产品费用"
        rewritten = await rag.rewrite_query(original_query)
        rewrite_package = request_scope.last_context_package
        assert rewritten == "改写后的检索查询"
        assert request_scope is active_context.get()
        assert request_scope.state is state
        assert request_scope.request_id == "isolated-request-id"
        assert request_scope.execution_id == execution_id
        assert request_scope.event_counter == 23
        assert rewrite_package is not None
        assert rewrite_package.diagnostics["agent"] == "knowledge_rag.rewrite"
        assert rewrite_package.diagnostics["storage_read_path"] == "query_only_no_storage"
        rewrite_task = QUERY_REWRITE_PROMPT.format(query=original_query)
        assert _messages(llm.calls[0], HumanMessage)[0].content == (
            f"<CurrentUser>\n{rewrite_task}\n</CurrentUser>"
        )

        content = "仅审查这段客服回答"
        compliance = await checker.llm_check(content, state=state)
        compliance_package = request_scope.last_context_package
        assert compliance.passed is True
        assert request_scope is active_context.get()
        assert request_scope.state is state
        assert request_scope.request_id == "isolated-request-id"
        assert request_scope.execution_id == execution_id
        assert request_scope.event_counter == 23
        assert compliance_package is not None
        assert compliance_package.diagnostics["agent"] == "compliance_checker"
        assert compliance_package.diagnostics["storage_read_path"] == "query_only_no_storage"
        compliance_task = f"请审查以下客服回复内容的合规性：\\n\\n{content}"
        assert _messages(llm.calls[1], HumanMessage)[0].content == (
            f"<CurrentUser>\n{compliance_task}\n</CurrentUser>"
        )
        assert "PRIVATE" not in "\n".join(
            str(message.content) for message in rewrite_package.messages + compliance_package.messages
        )

    assert storage.calls == []
    user_memory.profile_cards.assert_not_awaited()
    user_memory.retrieve.assert_not_awaited()
    assert [observation["scope"] for observation in llm.observations] == [request_scope, request_scope]
    assert [observation["request_id"] for observation in llm.observations] == [
        "isolated-request-id", "isolated-request-id",
    ]
    assert all(observation["execution_id"] == execution_id for observation in llm.observations)
    assert all(observation["event_counter"] == 23 for observation in llm.observations)
    assert llm.observations[0]["package"] is rewrite_package
    assert llm.observations[1]["package"] is compliance_package
    assert active_context.get() is None


@pytest.mark.asyncio
async def test_concurrent_isolated_agents_never_cross_active_request_owners():
    storage = QueryContextReadSpy()
    user_memory = SimpleNamespace(
        profile_cards=AsyncMock(return_value=[]),
        retrieve=AsyncMock(return_value=[]),
    )
    manager = ContextManager(
        event_store=storage,
        user_memory=user_memory,
        tokenizer=CharacterTokenizer(),
        default_model=PROFILE,
    )
    llm = ScopeObservingLLM()
    rag = KnowledgeRAGAgent(llm, LongTermMemory())
    checker = ComplianceCheckerAgent(llm)
    rewrite_state = {
        "session_id": "concurrent-session-a",
        "user_id": "concurrent-owner-a",
        "messages": [HumanMessage(content="PRIVATE owner A history")],
    }
    compliance_state = {
        "session_id": "concurrent-session-b",
        "user_id": "concurrent-owner-b",
        "messages": [HumanMessage(content="PRIVATE owner B history")],
    }

    async def rewrite_for_owner():
        async with manager.bind_request(
            rewrite_state["session_id"], rewrite_state["user_id"],
            request_id="request-a", state=rewrite_state,
        ) as scope:
            scope.event_counter = 7
            result = await rag.rewrite_query("owner A query")
            assert active_context.get() is scope
            assert scope.state is rewrite_state
            assert scope.request_id == "request-a"
            assert scope.event_counter == 7
            assert scope.last_context_package.diagnostics["agent"] == "knowledge_rag.rewrite"
            return scope, result

    async def compliance_for_owner():
        async with manager.bind_request(
            compliance_state["session_id"], compliance_state["user_id"],
            request_id="request-b", state=compliance_state,
        ) as scope:
            scope.event_counter = 17
            result = await checker.llm_check("owner B response", state=compliance_state)
            assert active_context.get() is scope
            assert scope.state is compliance_state
            assert scope.request_id == "request-b"
            assert scope.event_counter == 17
            assert scope.last_context_package.diagnostics["agent"] == "compliance_checker"
            return scope, result

    (rewrite_scope, rewritten), (compliance_scope, compliance) = await asyncio.gather(
        rewrite_for_owner(), compliance_for_owner()
    )

    assert rewrite_scope is not compliance_scope
    assert rewrite_scope.owner == ("concurrent-session-a", "concurrent-owner-a")
    assert compliance_scope.owner == ("concurrent-session-b", "concurrent-owner-b")
    assert rewrite_scope.request_id == "request-a"
    assert compliance_scope.request_id == "request-b"
    assert rewritten == "改写后的检索查询"
    assert compliance.passed is True
    assert storage.calls == []
    observations = {observation["request_id"]: observation for observation in llm.observations}
    assert observations["request-a"]["scope"] is rewrite_scope
    assert observations["request-a"]["owner"] == rewrite_scope.owner
    assert observations["request-a"]["event_counter"] == 7
    assert observations["request-b"]["scope"] is compliance_scope
    assert observations["request-b"]["owner"] == compliance_scope.owner
    assert observations["request-b"]["event_counter"] == 17
    assert all(
        "PRIVATE" not in str(message.content)
        for observation in llm.observations
        for message in observation["messages"]
    )
    assert active_context.get() is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("budget_env", "error_text", "secret_value"),
    [
        ({"SMARTCS_CONTEXT_SOFT_RATIO": "DO_NOT_LEAK_SECRET"}, "SMARTCS_CONTEXT_SOFT_RATIO", "DO_NOT_LEAK_SECRET"),
        (
            {
                "SMARTCS_CONTEXT_LIMIT": "1024",
                "SMARTCS_CONTEXT_HARD_RATIO": "0.85",
                "SMARTCS_CONTEXT_MAX_OUTPUT_TOKENS": "900",
                "SMARTCS_CONTEXT_RESERVE": "100",
                "SMARTCS_CONTEXT_SAFETY_MARGIN": "100",
            },
            "model limits leave no positive prompt budget",
            "900",
        ),
    ],
)
async def test_api_startup_rejects_invalid_context_budget_before_external_initialization(
    monkeypatch, budget_env, error_text, secret_value
):
    from unittest.mock import Mock

    from api import main as api

    for name, value in {
        "SMARTCS_CONTEXT_LIMIT": "8192",
        "SMARTCS_CONTEXT_MAX_OUTPUT_TOKENS": "1024",
        "SMARTCS_CONTEXT_RESERVE": "256",
        "SMARTCS_CONTEXT_SAFETY_MARGIN": "128",
        "SMARTCS_CONTEXT_SOFT_RATIO": "0.7",
        "SMARTCS_CONTEXT_HARD_RATIO": "0.85",
        **budget_env,
    }.items():
        monkeypatch.setenv(name, value)
    tracer_init = Mock()
    checkpoint_factory = Mock()
    database_factory = Mock()
    monkeypatch.setattr(api, "init_tracer", tracer_init)
    monkeypatch.setattr(api.CheckpointStore, "from_env", checkpoint_factory)
    monkeypatch.setattr(api.PlatformDatabase, "from_env", database_factory)

    with pytest.raises(ValueError, match=error_text) as error:
        async with api.lifespan(api.app):
            pytest.fail("invalid context budget reached a healthy lifespan")

    assert secret_value not in str(error.value)
    tracer_init.assert_not_called()
    checkpoint_factory.assert_not_called()
    database_factory.assert_not_called()


@pytest.mark.asyncio
async def test_warm_checkpoint_chat_uses_bounded_snapshot_and_preserves_recent_cache(tmp_path):
    from tests.test_orchestrator import _orchestrator, _state

    store = MemoryEventStore()
    cache = MemoryWorkingSetCache()
    manager = ContextManager(
        event_store=store,
        cache=cache,
        tokenizer=CharacterTokenizer(),
        default_model=PROFILE,
    )
    overrides = {
        "intent_router": {
            "primary_intent": "conversation",
            "secondary_intent": "identity",
            "confidence": 0.99,
            "entities": {},
            "suggested_agent": "conversation",
        },
        "default": "这是测试客服回答。",
    }
    orchestrator, _, _, _, _ = _orchestrator(tmp_path, overrides=overrides)
    orchestrator.checkpoint_store = store
    orchestrator.context_manager = manager
    synchronized_message_counts = []
    original_synchronize = manager.synchronize_checkpoint

    async def record_synchronized_size(checkpoint):
        synchronized_message_counts.append(len(checkpoint.messages))
        return await original_synchronize(checkpoint)

    manager.synchronize_checkpoint = record_synchronized_size
    router_prompts = []
    conversation_responses = 0
    llm = orchestrator.llm
    original_ainvoke = llm.ainvoke

    async def capture_router_prompt(messages):
        nonlocal conversation_responses
        system = "\n".join(
            str(message.content) for message in messages if isinstance(message, SystemMessage)
        )
        if "意图识别Agent" in system:
            router_prompts.append(list(messages))
        if CONVERSATION_SYSTEM_PROMPT in system:
            await original_ainvoke(messages)
            conversation_responses += 1
            return AIMessage(content=f"这是第{conversation_responses}轮测试客服回答。")
        return await original_ainvoke(messages)

    llm.ainvoke = capture_router_prompt
    last_request = None
    last_result = None
    for turn in range(50):
        last_request = {
            **_state(f"普通对话第{turn}轮", session_id="warm-session"),
            "client_request_id": f"warm-request-{turn}",
        }
        last_result = await orchestrator.ainvoke(last_request)

    assert last_result is not None
    assert len(router_prompts) == 50
    final_router_human = _messages({"messages": router_prompts[-1]}, HumanMessage)[0].content
    assert (
        "<CurrentUser>\n上一轮意图: conversation\n\n用户消息: 普通对话第49轮\n</CurrentUser>"
        in final_router_human
    )
    assert store.history_calls == 0
    assert synchronized_message_counts
    assert max(synchronized_message_counts) <= 20

    owner_key = ("warm-session", "user_002")
    recent = cache.values[owner_key]["recent_messages"]
    assert len(recent) >= 4
    assert recent[-2] == {"role": "user", "content": "普通对话第49轮"}
    assert recent[-1]["role"] == "assistant"

    calls_before_replay = llm.call_count
    replay = await orchestrator.ainvoke(last_request)
    assert replay["final_response"] == last_result["final_response"]
    assert llm.call_count == calls_before_replay

    resumed = await orchestrator.resume("warm-session", "user_002", "warm-request-49")
    assert resumed["final_response"] == last_result["final_response"]
    assert len(resumed["messages"]) <= 20
    assert llm.call_count == calls_before_replay
    assert store.history_calls == 0

    interrupted_request = {
        **_state("中断后继续的轮次", session_id="warm-session"),
        "client_request_id": "warm-interrupted-request",
    }
    original_route = orchestrator._route_intent

    async def fail_before_routing(_state):
        raise RuntimeError("injected interruption before routing")

    orchestrator._route_intent = fail_before_routing
    with pytest.raises(RuntimeError, match="injected interruption"):
        await orchestrator.ainvoke(interrupted_request)
    orchestrator._route_intent = original_route
    assert store.checkpoints[owner_key].current_stage == "PREPARED"
    resumed_running = await orchestrator.resume(
        "warm-session", "user_002", "warm-interrupted-request"
    )
    assert resumed_running["final_response"]
    calls_after_running_resume = llm.call_count
    assert any(
        "<CurrentUser>\n上一轮意图: conversation\n\n用户消息: 中断后继续的轮次\n</CurrentUser>" in
        _messages({"messages": prompt}, HumanMessage)[0].content
        for prompt in router_prompts
    )
    assert store.history_calls == 0

    checkpoint = store.checkpoints[owner_key]
    fenced = AgentCheckpoint.model_validate({
        **checkpoint.model_dump(),
        "current_stage": "ROUTED",
        "status": "running",
    })
    store.checkpoints[owner_key] = fenced
    with pytest.raises(CheckpointConflict, match="unfinished request exists"):
        await orchestrator.ainvoke({
            **_state("另一个新请求", session_id="warm-session"),
            "client_request_id": "must-be-fenced",
        })
    assert llm.call_count == calls_after_running_resume
    assert store.history_calls == 0


@pytest.mark.asyncio
async def test_tool_executor_persists_full_results_and_replays_ledger_without_second_write(tmp_path):
    store = MemoryEventStore()
    manager = ContextManager(event_store=store, default_model=PROFILE)
    server = MCPToolServer()
    side_effects = []

    async def write_tool(user_id: str, value: str):
        side_effects.append((user_id, value))
        return {"stored": value, "user_id": user_id}

    server.register_tool(ToolDefinition(
        name="test_write",
        description="deterministic write test",
        input_schema={"type": "object", "properties": {"user_id": {"type": "string"}, "value": {"type": "string"}}},
        handler=write_tool,
        operation_type="write",
    ))
    executor = ToolExecutor(server, ledger=ExecutionLedger(tmp_path / "ledger.sqlite"))

    async with manager.bind_request("tool-session", "owner-a", request_id="tool-request"):
        first = await executor.execute(
            "test_write", {"user_id": "owner-a", "value": "persisted-full-result"},
            ToolExecutionContext(confirmed=True, idempotency_key="stable-write-key"),
        )
        replay = await executor.execute(
            "test_write", {"user_id": "owner-a", "value": "persisted-full-result"},
            ToolExecutionContext(confirmed=True, idempotency_key="stable-write-key"),
        )
        package = await manager.build(
            "tool-session", "owner-a", "knowledge_rag", "后续问题",
            state={"session_id": "tool-session", "user_id": "owner-a", "messages": [HumanMessage(content="后续问题")]},
        )

    assert first.success is True and first.replayed is False
    assert replay.success is True and replay.replayed is True
    assert side_effects == [("owner-a", "persisted-full-result")]
    events = store.events[("tool-session", "owner-a")]
    assert [event["event_type"] for event in events] == ["TOOL_CALL", "TOOL_RESULT", "TOOL_CALL", "TOOL_RESULT"]
    assert "persisted-full-result" in json.dumps(events[1]["payload"], ensure_ascii=False)
    prompt = "\n".join(str(message.content) for message in package.messages)
    assert "persisted-full-result" in prompt
    assert package.total_tokens <= PROFILE.prompt_budget


@pytest.mark.asyncio
async def test_cold_restore_preserves_archive_and_pending_identifiers_without_owner_leak():
    store = MemoryEventStore()
    session_id, user_id = "cold-session-a", "owner-a"
    action = {
        "type": "refund_create",
        "order_id": "ORD-20260801-0002",
        "user_id": user_id,
        "amount": 12.5,
        "refund_mode": "refund_only",
        "reason": "用户申请退款",
        "idempotency_key": "refund:cold-session-a:ORD-20260801-0002",
        "arguments": {
            "order_id": "ORD-20260801-0002", "user_id": user_id, "reason": "用户申请退款",
        },
    }
    await store.append_event(session_id, user_id, "USER_MESSAGE", {"role": "user", "content": "确认退款"}, event_key="source-user")
    await store.append_event(session_id, user_id, "STATE_CHANGE", {"pending_action": action, "order_id": action["order_id"]}, event_key="pending-state")
    store.digests[(session_id, user_id)] = {
        "version": 3,
        "summary_event_seq": 2,
        "rolling_summary": "此前已完成退款资格评估，等待用户确认。",
        "archive_summary": {"provenance": [{"seq": 2, "event_type": "STATE_CHANGE"}], "resolved_topics": ["退款评估"]},
        "protected_fields": {"pending_action": action},
    }
    manager = ContextManager(event_store=store, default_model=PROFILE)

    async with manager.bind_request(session_id, user_id, request_id="resume-confirm", last_event_seq=2):
        restored = await manager.build(
            session_id, user_id, "refund_handler", "确认退款",
            state={"session_id": session_id, "user_id": user_id, "messages": [HumanMessage(content="确认退款")]},
        )
    async with manager.bind_request("cold-session-b", "owner-b", request_id="other-owner"):
        isolated = await manager.build(
            "cold-session-b", "owner-b", "conversation", "你好",
            state={"session_id": "cold-session-b", "user_id": "owner-b", "messages": [HumanMessage(content="你好")]},
        )

    rendered = "\n".join(str(message.content) for message in restored.messages)
    assert restored.protected_fields["pending_action"]["order_id"] == action["order_id"]
    assert action["idempotency_key"] in rendered
    assert "此前已完成退款资格评估" in rendered
    assert "退款评估" in rendered
    assert action["order_id"] not in "\n".join(str(message.content) for message in isolated.messages)
    with pytest.raises(CheckpointOwnershipError):
        await store.load_working_set(session_id, "owner-b")


@pytest.mark.asyncio
async def test_oversized_confirmation_admission_keeps_waiting_refund_resumable_and_write_exactly_once(tmp_path):
    from tests.test_orchestrator import _orchestrator, _state

    store = MemoryEventStore()
    tiny_profile = ModelProfile(
        name="tiny-admission-profile",
        provider="test",
        context_limit=6144,
        max_output_tokens=256,
        reserve=64,
        safety_margin=64,
        soft_ratio=0.7,
        hard_ratio=0.9,
        recent_messages=4,
        compression_attempts=4,
    )
    manager = ContextManager(
        event_store=store,
        tokenizer=CharacterTokenizer(),
        default_model=tiny_profile,
    )
    admission_builds = []
    original_build = manager.build

    async def record_build(*args, **kwargs):
        admission_builds.append({
            "agent": args[2],
            "current_message": args[3],
            "state": kwargs.get("state"),
            "system_prompt": kwargs.get("system_prompt"),
            "task_message": kwargs.get("task_message"),
        })
        return await original_build(*args, **kwargs)

    manager.build = record_build
    orchestrator, _, _, executor, repository = _orchestrator(tmp_path)
    orchestrator.checkpoint_store = store
    orchestrator.context_manager = manager

    prepared = {
        **_state("帮我退款 ORD-20260801-0002", session_id="admission-session"),
        "client_request_id": "refund-prepare",
    }
    await orchestrator.ainvoke(prepared)
    waiting = await store.load("admission-session", "user_002")
    assert waiting is not None and waiting.status == "waiting"
    pending_action = waiting.pending_action
    assert pending_action and pending_action["order_id"] == "ORD-20260801-0002"
    assert repository.get_order("ORD-20260801-0002")["refunds"] == []
    calls_before_oversized_input = orchestrator.llm.call_count

    oversized = {
        **_state("确认退款" + "中" * 20_000, session_id="admission-session"),
        "client_request_id": "oversized-confirmation",
    }
    with pytest.raises(ContextOverflowError):
        await orchestrator.ainvoke(oversized)

    from agents.intent_router import INTENT_SYSTEM_PROMPT

    admission = admission_builds[-1]
    assert admission["agent"] == "intent_router"
    assert admission["system_prompt"] == INTENT_SYSTEM_PROMPT
    assert admission["current_message"] == oversized["messages"][-1].content
    assert admission["task_message"] == (
        f"上一轮意图: refund_handler\n\n用户消息: {oversized['messages'][-1].content}"
    )
    assert admission["state"]["pending_action"] == pending_action
    assert pending_action["idempotency_key"] in json.dumps(admission["state"]["pending_action"], ensure_ascii=False)
    unchanged = await store.load("admission-session", "user_002")
    assert unchanged is not None
    assert unchanged.version == waiting.version
    assert unchanged.status == "waiting"
    assert unchanged.context["request_id"] == "refund-prepare"
    assert unchanged.pending_action == pending_action
    assert repository.get_order("ORD-20260801-0002")["refunds"] == []
    assert orchestrator.llm.call_count == calls_before_oversized_input
    assert not any(
        event["event_type"] == "USER_MESSAGE"
        and event["payload"].get("content") == oversized["messages"][-1].content
        for event in store.events[("admission-session", "user_002")]
    )
    assert active_context.get() is None

    confirmation = {
        **_state("确认退款", session_id="admission-session"),
        "client_request_id": "short-confirmation",
    }
    result = await orchestrator.ainvoke(confirmation)
    assert "退款申请已提交" in result["final_response"]
    refunds = repository.get_order("ORD-20260801-0002")["refunds"]
    assert len(refunds) == 1
    ledger_result = executor.ledger.get(pending_action["idempotency_key"])
    assert ledger_result is not None and ledger_result["status"] == "completed"
    calls_after_success = orchestrator.llm.call_count

    replay = await orchestrator.ainvoke(confirmation)
    assert replay["final_response"] == result["final_response"]
    assert len(repository.get_order("ORD-20260801-0002")["refunds"]) == 1
    assert orchestrator.llm.call_count == calls_after_success


@pytest.mark.asyncio
async def test_router_admission_rejects_borderline_context_before_prepared_write(tmp_path):
    import tiktoken

    from tests.test_orchestrator import _orchestrator, _state

    store = MemoryEventStore()
    tokenizer = tiktoken.get_encoding("cl100k_base")
    profile = ModelProfile(
        name="cl100k-admission-borderline",
        provider="test",
        context_limit=6000,
        max_output_tokens=100,
        reserve=50,
        safety_margin=50,
        soft_ratio=0.7,
        hard_ratio=0.9,
        recent_messages=4,
        compression_attempts=4,
    )
    manager = ContextManager(event_store=store, tokenizer=tokenizer, default_model=profile)
    orchestrator, session_store, _, executor, repository = _orchestrator(tmp_path)
    orchestrator.checkpoint_store = store
    orchestrator.context_manager = manager

    prepared = {
        **_state("帮我退款 ORD-20260801-0002", session_id="borderline-admission-session"),
        "client_request_id": "borderline-prepare",
    }
    await orchestrator.ainvoke(prepared)
    session_id, user_id = "borderline-admission-session", "user_002"
    waiting = await store.load(session_id, user_id)
    assert waiting is not None and waiting.status == "waiting" and waiting.pending_action
    session_state = waiting.context["session_state"]
    current_input = "x " * 4800
    assert len(tokenizer.encode(current_input)) >= 4800
    old_policy_state = {
        "session_id": session_id,
        "user_id": user_id,
        "messages": [HumanMessage(content=current_input)],
        "session_state": session_state,
        "pending_action": session_state["pending_action"],
        "accumulated_entities": session_state["accumulated_entities"],
    }
    old_policy_package = await manager.build(
        session_id,
        user_id,
        "compliance_checker",
        current_input,
        model=profile,
        state=old_policy_state,
        system_prompt=COMPLIANCE_SYSTEM_PROMPT,
    )
    assert old_policy_package.total_tokens <= profile.prompt_budget

    checkpoint_before = store.checkpoints[(session_id, user_id)].model_dump()
    events_before = deepcopy(store.events[(session_id, user_id)])
    turn_count_before = (await session_store.get_state(session_id)).turn_count
    calls_before = orchestrator.llm.call_count
    refunds_before = repository.get_order("ORD-20260801-0002")["refunds"]
    oversized = {
        **_state(current_input, session_id=session_id),
        "client_request_id": "borderline-overflow",
    }
    with pytest.raises(ContextOverflowError):
        await orchestrator.ainvoke(oversized)

    assert store.checkpoints[(session_id, user_id)].model_dump() == checkpoint_before
    assert store.events[(session_id, user_id)] == events_before
    assert (await session_store.get_state(session_id)).turn_count == turn_count_before
    assert orchestrator.llm.call_count == calls_before
    assert repository.get_order("ORD-20260801-0002")["refunds"] == refunds_before == []
    assert executor.ledger.get(waiting.pending_action["idempotency_key"]) is None

    confirmation = {
        **_state("确认退款", session_id=session_id),
        "client_request_id": "borderline-short-confirmation",
    }
    result = await orchestrator.ainvoke(confirmation)
    assert "退款申请已提交" in result["final_response"]
    assert len(repository.get_order("ORD-20260801-0002")["refunds"]) == 1
    calls_after_success = orchestrator.llm.call_count
    replay = await orchestrator.ainvoke(confirmation)
    assert replay["final_response"] == result["final_response"]
    assert len(repository.get_order("ORD-20260801-0002")["refunds"]) == 1
    assert orchestrator.llm.call_count == calls_after_success


@pytest.mark.asyncio
async def test_request_binding_resets_after_llm_failure():
    manager = ContextManager(default_model=PROFILE)
    llm = RecordingLLM()

    async def fail(_messages, **_kwargs):
        raise RuntimeError("injected LLM failure")

    llm.ainvoke = fail
    state = {"session_id": "failure-session", "user_id": "owner", "messages": [HumanMessage(content="触发失败")], "sub_results": {}}
    async with manager.bind_request("failure-session", "owner", request_id="failure", state=state):
        binding = active_context.get()
        try:
            await ConversationAgent(llm).process(state)
        except RuntimeError as exc:
            assert "injected LLM failure" in str(exc)
        else:
            pytest.fail("the injected model failure did not propagate")
        assert active_context.get() is binding
    assert active_context.get() is None


@pytest.mark.asyncio
async def test_sidebar_default_operation_ids_sync_only_the_synthetic_pair(monkeypatch):
    from api import main as api

    store = MemoryEventStore()
    cache = MemoryWorkingSetCache()
    manager = ContextManager(event_store=store, cache=cache, default_model=PROFILE)
    monkeypatch.setattr(api, "checkpoint_store", store)
    monkeypatch.setattr(api, "context_manager", manager)
    order = {
        "found": True,
        "order_id": "ORD-20260801-0002",
        "status": "shipped",
        "status_label": "已发货",
        "product": "测试商品",
        "user_id": "sidebar-owner",
    }

    await api._persist_order_query_context("sidebar-default-session", "sidebar-owner", order)
    await api._persist_order_query_context("sidebar-default-session", "sidebar-owner", order)

    events = store.events[("sidebar-default-session", "sidebar-owner")]
    operation_keys = {
        event["event_key"].rsplit(":", 1)[0]
        for event in events
        if event["event_type"] == "TOOL_CALL"
    }
    assert len(operation_keys) == 2
    assert [event["event_type"] for event in events].count("TOOL_CALL") == 2
    assert [event["event_type"] for event in events].count("TOOL_RESULT") == 2
    assert [event["event_type"] for event in events].count("USER_MESSAGE") == 2
    assert all(
        event["payload"].get("synthetic") is True
        for event in events if event["event_type"] == "USER_MESSAGE"
    )
    recent = cache.values[("sidebar-default-session", "sidebar-owner")]["recent_messages"]
    assert recent[-4:] == [
        {"role": "user", "content": "查询订单 ORD-20260801-0002"},
        {"role": "assistant", "content": "上一轮已查询订单 ORD-20260801-0002，状态：已发货，商品：测试商品。"},
        {"role": "user", "content": "查询订单 ORD-20260801-0002"},
        {"role": "assistant", "content": "上一轮已查询订单 ORD-20260801-0002，状态：已发货，商品：测试商品。"},
    ]
    assert store.history_calls == 0

    cp = store.checkpoints[("sidebar-default-session", "sidebar-owner")]
    store.checkpoints[("sidebar-default-session", "sidebar-owner")] = AgentCheckpoint.model_validate({
        **cp.model_dump(),
        "current_stage": "ROUTED",
        "status": "running",
    })
    before_rejected_sidebar_read = deepcopy(store.events[("sidebar-default-session", "sidebar-owner")])
    with pytest.raises(CheckpointConflict, match="unfinished chat request owns this session"):
        await api._persist_order_query_context("sidebar-default-session", "sidebar-owner", order)
    assert store.events[("sidebar-default-session", "sidebar-owner")] == before_rejected_sidebar_read
    assert store.history_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["chat", "resume", "legacy-chat"])
async def test_chat_routes_preserve_context_errors_for_safe_global_handler(monkeypatch, endpoint):
    from api import main as api

    error = ContextOverflowError("internal prompt details must stay private")
    orchestrator = SimpleNamespace(
        ainvoke=AsyncMock(side_effect=error),
        resume=AsyncMock(side_effect=error),
    )
    monkeypatch.setattr(api, "chat_orchestrator", orchestrator)
    monkeypatch.setattr(api, "checkpoint_store", SimpleNamespace())
    monkeypatch.setattr(api, "_owned_session", AsyncMock(return_value={"session_id": "context-error-session"}))
    monkeypatch.setattr(
        api.app.state,
        "platform_sessions",
        SimpleNamespace(touch=AsyncMock()),
        raising=False,
    )
    user = SimpleNamespace(account_id=1, business_user_id="context-error-owner")

    with pytest.raises(ContextOverflowError):
        if endpoint in {"chat", "legacy-chat"}:
            if endpoint == "legacy-chat":
                monkeypatch.setattr(api, "checkpoint_store", None)
                monkeypatch.setattr(
                    api,
                    "session_store",
                    SimpleNamespace(
                        add_message=AsyncMock(),
                        get_history=AsyncMock(return_value=[]),
                    ),
                )
            await api.chat(
                api.ChatRequest(message="oversized request", session_id="context-error-session"),
                user,
            )
        else:
            await api.resume_checkpoint(
                "context-error-session", api.ResumeRequest(), user
            )

    response = await api.context_error(None, error)
    assert response.status_code == 409
    assert b"context request cannot be assembled" in response.body
    assert b"internal prompt details" not in response.body


@pytest.mark.asyncio
async def test_two_sidebar_order_reads_append_independent_events_and_preserve_old_chat_receipt(monkeypatch):
    from api import main as api
    from auth.context import UserContext

    store = MemoryEventStore()
    session_id, user_id = "sidebar-session", "business-user-1"
    request_id = "chat-request-1"
    original_message = "请介绍一下 SmartCS"
    request_hash = hashlib.sha256(original_message.encode()).hexdigest()
    receipt = {
        "final_response": "聊天答复已完成",
        "intent": "conversation",
        "compliance_passed": True,
        "client_request_id": request_id,
        "session_id": session_id,
    }
    store.receipts[(session_id, request_id)] = (request_hash, receipt)
    state = {
        "intent": "conversation",
        "sub_results": {},
        "compliance_passed": True,
        "final_response": receipt["final_response"],
        "current_agent": "orchestrator",
        "needs_clarification": False,
    }
    session_state = ConversationState(last_intent="conversation", turn_count=1).to_dict()
    cp = AgentCheckpoint(
        session_id=session_id,
        user_id=user_id,
        intent="conversation",
        current_stage="FINISHED",
        status="finished",
        messages=[],
        context={
            "workflow_version": 1,
            "request_id": request_id,
            "request_hash": request_hash,
            "session_state": session_state,
            "state": state,
        },
        version=1,
        last_event_seq=0,
    )
    store.checkpoints[(session_id, user_id)] = cp
    await store.append_event(session_id, user_id, "USER_MESSAGE", {"role": "user", "content": original_message}, event_key="chat:user")
    await store.append_event(session_id, user_id, "ASSISTANT_MESSAGE", {"role": "assistant", "content": receipt["final_response"]}, event_key="chat:assistant")
    store.checkpoints[(session_id, user_id)] = AgentCheckpoint.model_validate({
        **cp.model_dump(), "last_event_seq": 2,
    })

    manager = ContextManager(event_store=store, user_memory=MemorySpy(), default_model=PROFILE)
    monkeypatch.setattr(api, "checkpoint_store", store)
    monkeypatch.setattr(api, "context_manager", manager)
    order = {
        "found": True,
        "order_id": "ORD-20260801-0002",
        "status": "shipped",
        "status_label": "已发货",
        "product": "测试商品",
        "user_id": user_id,
    }
    await api._persist_order_query_context(
        session_id, user_id, order, tool_arguments={"order_id": order["order_id"]},
        tool_result=order, event_operation_id="sidebar-read-1",
    )
    # Retrying one sidebar operation reuses stable event keys; a new operation
    # gets its own event group and remains visible in history.
    await api._persist_order_query_context(
        session_id, user_id, order, tool_arguments={"order_id": order["order_id"]},
        tool_result=order, event_operation_id="sidebar-read-1",
    )
    await api._persist_order_query_context(
        session_id, user_id, order, tool_arguments={"order_id": order["order_id"]},
        tool_result=order, event_operation_id="sidebar-read-2",
    )

    events = store.events[(session_id, user_id)]
    assert [event["event_type"] for event in events].count("TOOL_CALL") == 2
    assert [event["event_type"] for event in events].count("TOOL_RESULT") == 2
    assert [event["event_type"] for event in events].count("USER_MESSAGE") == 3
    assert [event["event_type"] for event in events].count("ASSISTANT_MESSAGE") == 3
    assert all(event["payload"].get("synthetic") is True for event in events if event["event_type"] == "USER_MESSAGE" and event["event_key"] not in {"chat:user"})
    assert store.checkpoints[(session_id, user_id)].context.get("request_id") is None

    owner = UserContext(account_id=1, username="owner", business_user_id=user_id)
    monkeypatch.setattr(api.app.state, "platform_sessions", SimpleNamespace(
        get_owned=AsyncMock(return_value={"session_id": session_id}),
    ), raising=False)
    history = await api.get_history(session_id, owner)
    assert all(set(message) == {"role", "content"} for message in history["messages"])
    assert len(history["messages"]) == 6

    llm = MockLLM()
    orchestrator = ChatOrchestrator(
        llm,
        SessionStore(ShortTermMemory(redis_url="redis://127.0.0.1:6399/0", redis_retry_cooldown=60)),
        LongTermMemory(),
        checkpoint_store=store,
        context_manager=manager,
    )
    replay = await orchestrator.ainvoke({
        "session_id": session_id,
        "user_id": user_id,
        "client_request_id": request_id,
        "messages": [HumanMessage(content=original_message)],
    })
    assert replay["final_response"] == receipt["final_response"]
    assert llm.call_count == 0
    manager.user_memory.process_message.assert_not_awaited()


class _BlockedMemoryService:
    """Records durable enqueues but blocks any application attempt forever."""

    def __init__(self, inner, release: asyncio.Event) -> None:
        self.inner = inner
        self.release = release
        self.enqueued: list[dict] = []

    async def process_message(self, *args, **kwargs):
        result = await self.inner.process_message(*args, **kwargs)
        self.enqueued.append(result)
        return result

    async def process_pending(self, *args, **kwargs):
        await self.release.wait()
        return await self.inner.process_pending(*args, **kwargs)

    async def drain_pending(self, *args, **kwargs):
        await self.release.wait()
        return await self.inner.drain_pending(*args, **kwargs)


@pytest.mark.asyncio
async def test_chat_tail_enqueues_memory_but_never_awaits_candidate_application(tmp_path):
    from memory.user_memory_worker import UserMemoryWorker
    from tests.test_orchestrator import _orchestrator, _state

    store = MemoryEventStore()
    repository = InMemoryUserMemoryRepository()
    store.source_repository = repository
    inner = UserMemoryService(repository=repository)
    release = asyncio.Event()
    blocked = _BlockedMemoryService(inner, release)

    manager = ContextManager(
        event_store=store,
        cache=MemoryWorkingSetCache(),
        tokenizer=CharacterTokenizer(),
        default_model=PROFILE,
        user_memory=blocked,
    )
    overrides = {
        "intent_router": {
            "primary_intent": "conversation",
            "secondary_intent": "identity",
            "confidence": 0.99,
            "entities": {},
            "suggested_agent": "conversation",
        },
        "default": "这是测试客服回答。",
    }
    orchestrator, _, _, _, _ = _orchestrator(tmp_path, overrides=overrides)
    orchestrator.checkpoint_store = store
    orchestrator.context_manager = manager

    state = {
        **_state("Please keep it concise.", session_id="memory-tail-session"),
        "client_request_id": "memory-tail-request-1",
    }
    # If the chat request awaited candidate application this bounded wait fails.
    result = await asyncio.wait_for(orchestrator.ainvoke(state), timeout=30)
    assert result["final_response"]
    assert release.is_set() is False

    # The durable candidate was queued before response completion.
    assert len(blocked.enqueued) == 1
    pending = [row for row in repository.candidates.values() if row["decision"] == "PENDING"]
    assert pending and pending[0]["user_id"] == "user_002"
    assert repository.cards == []

    # The application-owned background worker later applies the candidate.
    worker = UserMemoryWorker(inner)
    assert await worker.run_once() is True
    assert len(repository.cards) == 1
    release.set()


@pytest.mark.asyncio
async def test_memory_enqueue_failure_never_replays_model_or_business_execution(tmp_path):
    from tests.test_orchestrator import _orchestrator, _state

    store = MemoryEventStore()
    repository = InMemoryUserMemoryRepository()
    store.source_repository = repository

    class FailingMemoryService:
        async def process_message(self, *_args, **_kwargs):
            raise RuntimeError("injected enqueue failure")

        async def process_pending(self, *_args, **_kwargs):
            raise AssertionError("chat request must not await memory application")

    manager = ContextManager(
        event_store=store,
        cache=MemoryWorkingSetCache(),
        tokenizer=CharacterTokenizer(),
        default_model=PROFILE,
        user_memory=FailingMemoryService(),
    )
    overrides = {
        "intent_router": {
            "primary_intent": "conversation",
            "secondary_intent": "identity",
            "confidence": 0.99,
            "entities": {},
            "suggested_agent": "conversation",
        },
        "default": "这是测试客服回答。",
    }
    orchestrator, _, _, _, _ = _orchestrator(tmp_path, overrides=overrides)
    orchestrator.checkpoint_store = store
    orchestrator.context_manager = manager

    state = {
        **_state("Please reply in English.", session_id="memory-failure-session"),
        "client_request_id": "memory-failure-request-1",
    }
    result = await asyncio.wait_for(orchestrator.ainvoke(state), timeout=30)
    assert result["final_response"]
    calls_after_turn = orchestrator.llm.call_count
    assert repository.candidates == {}

    # The durable receipt replays without re-running any model/business step.
    replay = await orchestrator.ainvoke(state)
    assert replay["final_response"] == result["final_response"]
    assert orchestrator.llm.call_count == calls_after_turn
    assert repository.candidates == {}
