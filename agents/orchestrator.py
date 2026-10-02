"""Explicit application orchestrator used by the production chat path."""

from __future__ import annotations

import os
import hashlib
import logging
import uuid
from dataclasses import asdict
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage
from langchain_openai import ChatOpenAI

from agents.compliance_checker import ComplianceCheckerAgent
from agents.conversation import ConversationAgent
from agents.intent_router import INTENT_SYSTEM_PROMPT, IntentRouterAgent
from agents.knowledge_rag import KnowledgeRAGAgent
from agents.refund_handler import RefundHandlerAgent
from agents.ticket_handler import TicketHandlerAgent
from context.manager import ContextManager
from memory.long_term import LongTermMemory
from memory.session_store import ConversationState, SessionStore
from mcp.mcp_server import MCPToolServer
from mcp.tool_execution import ToolExecutionContext, ToolExecutor
from tracing.otel_config import create_traced_chat_openai
from tickets.service import canonical_ticket_payload_hash
from checkpoint.models import (
    AgentCheckpoint, CheckpointConflict, CheckpointCorrupt, active_checkpoint,
)
from checkpoint.store import CheckpointStore


logger = logging.getLogger(__name__)


def _encode_messages(messages):
    encoded = []
    for message in messages:
        if type(message) not in (HumanMessage, AIMessage) or not isinstance(message.content, str):
            raise CheckpointCorrupt("only text human/assistant messages can be checkpointed")
        encoded.append({"role": "user" if isinstance(message, HumanMessage) else "assistant", "content": message.content})
    return encoded


def _decode_messages(messages):
    decoded = []
    for message in messages:
        role = message["role"] if isinstance(message, dict) else message.role
        content = message["content"] if isinstance(message, dict) else message.content
        decoded.append((HumanMessage if role == "user" else AIMessage)(content=content))
    return decoded


def _validate_runtime_snapshot(checkpoint):
    try:
        if checkpoint.context.get("workflow_version") != 1:
            raise ValueError("unsupported workflow version")
        session = ConversationState.from_dict(checkpoint.context["session_state"])
        if session.pending_action != checkpoint.pending_action:
            raise ValueError("inconsistent pending action")
        state = checkpoint.context["state"]
        fields = {
            "intent", "sub_results", "compliance_passed", "final_response", "current_agent", "needs_clarification"
        }
        if not isinstance(state, dict) or set(state) - fields:
            raise ValueError("invalid workflow state")
        if checkpoint.context.get("request_id") and set(state) != fields:
            raise ValueError("incomplete workflow state")
        for key in ("intent", "final_response", "current_agent"):
            if key in state and not isinstance(state[key], str):
                raise ValueError("invalid text field")
        for key in ("compliance_passed", "needs_clarification"):
            if key in state and type(state[key]) is not bool:
                raise ValueError("invalid boolean field")
        if "sub_results" in state and not isinstance(state["sub_results"], dict):
            raise ValueError("invalid sub-results")
    except (ValueError, TypeError, KeyError) as exc:
        raise CheckpointCorrupt("invalid or incompatible workflow checkpoint") from exc


class _CheckpointRun:
    """One request's durable cursor and tool plan, bound with a ContextVar."""

    def __init__(self, owner, checkpoint, state):
        self.owner, self.checkpoint, self.state = owner, checkpoint, state

    async def save(self, stage=None):
        cp = self.checkpoint
        context = dict(cp.context)
        session = (await self.owner.session_store.get_state(cp.session_id)).to_dict()
        # Explicit allowlist: no callbacks, LLM metadata, credentials or reasoning trace.
        context["state"] = {key: self.state[key] for key in (
            "intent", "sub_results", "compliance_passed", "final_response", "current_agent", "needs_clarification"
        ) if key in self.state}
        context["session_state"] = session
        stage = stage or cp.current_stage
        self.checkpoint = await self.owner.checkpoint_store.update(AgentCheckpoint.model_validate({
            **cp.model_dump(), "intent": self.state.get("intent", ""),
            "current_stage": stage, "status": {"FINISHED": "finished", "WAIT_CONFIRM": "waiting"}.get(stage, "running"),
            "pending_action": session.get("pending_action"), "context": context,
            "messages": (
                [{"role": "assistant", "content": self.state["messages"][-1].content}]
                if stage in {"WAIT_CONFIRM", "FINISHED"}
                and self.state.get("messages")
                and isinstance(self.state["messages"][-1], AIMessage)
                else []
            ),
        }))
        # The checkpoint store returns only its bounded compatibility message
        # snapshot. Sync it instead of copying the full in-flight conversation at
        # every workflow stage; ContextManager augments its working set as needed.
        await self.owner._synchronize_checkpoint(self.checkpoint)

    async def ticket_plan(self, create):
        plan = self.checkpoint.context.get("ticket_plan")
        if plan is None:
            plan = await create()
            fields = {"action", "ticket_type", "priority", "summary", "details", "ticket_id"}
            if not isinstance(plan, dict) or any(not isinstance(v, str) for k, v in plan.items() if k in fields):
                raise CheckpointCorrupt("invalid ticket plan; no write was attempted")
            plan = {k: v for k, v in plan.items() if k in fields}
            self.checkpoint.context["ticket_plan"] = plan
            await self.save()
        return dict(plan)

    async def before_write(self, name, arguments, context):
        slot = self.checkpoint.current_stage
        effects = self.checkpoint.context.setdefault("effects", {})
        plan = {"name": name, "arguments": arguments, "authorization": asdict(context),
                "confirmation_message": self.state["messages"][-1].content}
        previous = effects.get(slot)
        if previous is not None and previous != plan:
            raise CheckpointConflict("saved write plan differs; manual review required")
        effects[slot] = plan
        # Always fence again, including replay. Nothing may write if this save fails.
        await self.save()

        if previous is not None and self.owner.execution_reconciler is not None:
            self.owner.execution_reconciler.reconcile_key(context.idempotency_key)

    def after_write(self, result):
        if result.error_code in {"execution_in_progress", "execution_ledger_error", "idempotency_conflict", "timeout", "execution_error"}:
            raise CheckpointConflict("write outcome unresolved; reconcile the execution ledger before resume")


class ChatOrchestrator:
    """Run one chat request through intent, one business agent, and compliance."""

    def __init__(
        self,
        llm: ChatOpenAI,
        session_store: SessionStore,
        long_term_memory: LongTermMemory,
        mcp_server: MCPToolServer | None = None,
        tool_executor: ToolExecutor | None = None,
        checkpoint_store: CheckpointStore | None = None,
        execution_reconciler=None,
        retriever=None,
        context_manager: ContextManager | None = None,
    ) -> None:
        self.llm = llm
        self.session_store = session_store
        self.long_term_memory = long_term_memory
        self.mcp_server = mcp_server
        self.tool_executor = tool_executor
        self.checkpoint_store = checkpoint_store
        self.execution_reconciler = execution_reconciler
        self.context_manager = context_manager or ContextManager()
        self.intent_router = IntentRouterAgent(llm)
        self.conversation_agent = ConversationAgent(llm)
        self.retriever = retriever or long_term_memory.get_retriever(use_env=False)
        self.knowledge_agent = KnowledgeRAGAgent(
            llm, long_term_memory, retriever=self.retriever
        )
        self.ticket_agent = TicketHandlerAgent(llm, tool_executor=tool_executor)
        self.refund_agent = RefundHandlerAgent(llm, tool_executor, session_store)
        self.compliance_agent = ComplianceCheckerAgent(llm)

    async def _process_user_memory(
        self,
        session_id: str,
        user_id: str,
        request_id: str,
        content: str,
        checkpoint: AgentCheckpoint,
    ) -> None:
        """Enqueue only the durable source user turn after its receipt is saved.

        Candidate application is owned by the application's bounded background
        ``UserMemoryWorker`` (durable lease queue). The chat request never
        awaits application/consolidation, so chat latency cannot grow with
        memory work; enqueue failures stay idempotent and observable.
        """
        service = getattr(self.context_manager, "user_memory", None)
        enqueue = getattr(service, "process_message", None)
        if not callable(enqueue):
            return

        try:
            events = await self.checkpoint_store.recent_events(
                session_id,
                user_id,
                after_seq=max(0, checkpoint.last_event_seq - 1000),
                limit=1000,
            )
            prepared = [
                event for event in events
                if event.get("event_type") == "STATE_CHANGE"
                and event.get("payload", {}).get("request_id") == request_id
                and event.get("payload", {}).get("stage") == "PREPARED"
            ]
            if not prepared:
                return
            boundary = min(int(event.get("seq") or 0) for event in prepared)
            source = max(
                (
                    event for event in events
                    if event.get("event_type") == "USER_MESSAGE"
                    and not event.get("payload", {}).get("synthetic")
                    and int(event.get("seq") or 0) < boundary
                    and event.get("payload", {}).get("content") == content
                ),
                key=lambda event: int(event.get("seq") or 0),
                default=None,
            )
            if source is None:
                return

            enqueue_result = None
            for attempt in range(2):
                enqueue_result = await enqueue(
                    user_id, session_id, source["event_id"], content
                )
                if not enqueue_result or not enqueue_result.get("retryable"):
                    break
            if not enqueue_result or enqueue_result.get("retryable"):
                logger.warning("user-memory candidate enqueue deferred after bounded retry")
        except Exception as exc:
            # The chat receipt/checkpoint is authoritative and already durable.
            # Memory enqueue retries are idempotent and must never trigger
            # model/tool replay; the background worker owns later application.
            logger.warning("user-memory enqueue deferred (%s)", type(exc).__name__)

    async def _synchronize_checkpoint(self, checkpoint: AgentCheckpoint, messages=None) -> None:
        synchronize = getattr(self.context_manager, "synchronize_checkpoint", None)
        if callable(synchronize):
            projection = checkpoint
            if messages is not None:
                projection = AgentCheckpoint.model_validate({
                    **checkpoint.model_dump(),
                    "messages": _encode_messages(messages),
                })
            await synchronize(projection)

    async def _admit_incoming_context(
        self,
        session_id: str,
        user_id: str,
        content: str,
        session_state: dict[str, Any],
    ) -> None:
        """Fail closed on required context before a new checkpoint can become running.

        This is a pure context build: it does not invoke an LLM or mutate business
        state. In particular, an oversized confirmation leaves the prior
        WAIT_CONFIRM checkpoint and its authoritative pending action intact.
        """
        model = getattr(self.llm, "model_name", None) or getattr(self.llm, "model", None)
        last_intent = session_state.get("last_intent")
        task_message = f"用户消息: {content}"
        if last_intent:
            task_message = f"上一轮意图: {last_intent}\n\n{task_message}"
        admission_state = {
            "session_id": session_id,
            "user_id": user_id,
            "intent": "",
            "current_agent": "orchestrator",
            "needs_clarification": False,
            "compliance_passed": True,
            "messages": [HumanMessage(content=content)],
            "session_state": session_state,
            "pending_action": session_state.get("pending_action"),
            "accumulated_entities": session_state.get("accumulated_entities", {}),
            "sub_results": {"_session_context": session_state},
        }
        await self.context_manager.build(
            session_id,
            user_id,
            "intent_router",
            content,
            model=model if isinstance(model, str) else None,
            state=admission_state,
            system_prompt=INTENT_SYSTEM_PROMPT,
            task_message=task_message,
        )

    async def _prepare_state(self, state: dict[str, Any]) -> dict[str, Any]:
        session_id = str(state.get("session_id", "default"))
        context = (await self.session_store.get_state(session_id)).to_dict()

        return {
            **state,
            "current_agent": "orchestrator",
            "needs_clarification": False,
            "sub_results": {
                **state.get("sub_results", {}),
                "_session_context": context,
            },
        }

    async def _route_intent(self, state: dict[str, Any]) -> dict[str, Any]:
        updated = await self.intent_router.process(state)
        session_id = str(updated.get("session_id", "default"))
        intent = updated.get("intent", "knowledge_rag")
        intent_info = updated.get("sub_results", {}).get("intent_router", {})
        new_entities = intent_info.get("entities", {}) or {}

        context = (await self.session_store.get_state(session_id)).to_dict()
        accumulated = dict(context.get("accumulated_entities", {}))
        accumulated.update(new_entities)
        turn_count = context.get("turn_count", 0) + 1
        context = await self.session_store.update_state(
            session_id,
            last_intent=intent,
            accumulated_entities=accumulated,
            turn_count=turn_count,
        )

        sub_results = dict(updated.get("sub_results", {}))
        sub_results["_session_context"] = context.to_dict()
        confidence = intent_info.get("confidence", 1.0)
        if confidence < 0.7 or self._route_name(intent) == "clarification":
            return {
                **updated,
                "sub_results": sub_results,
                "needs_clarification": True,
                "final_response": (
                    "抱歉，我还不太确定您的具体需求。"
                    "您是想咨询产品信息、查询订单，还是办理退款/开户？"
                    "请补充说明，我来帮您处理。"
                ),
            }
        return {**updated, "sub_results": sub_results, "needs_clarification": False}

    @staticmethod
    def _route_name(intent: str) -> str:
        return {
            "conversation": "conversation",
            "knowledge_rag": "knowledge_rag",
            "order_query": "ticket_handler",
            "ticket_handler": "ticket_handler",
            "refund_handler": "refund_handler",
            "refund_request": "refund_handler",
            "refund_confirm": "refund_handler",
            "refund_cancel": "refund_handler",
            "compliance_checker": "compliance_check",
        }.get(intent, "clarification")

    async def _create_escalation_ticket(self, state: dict[str, Any]) -> str:
        if self.tool_executor is None:
            return ""
        session_id = str(state.get("session_id", "unknown"))
        user_id = str(state.get("user_id", "anonymous"))
        title = "合规审查转人工"
        description = f"session_id={session_id}, compliance_failed=true"
        request_payload_hash = canonical_ticket_payload_hash(
            user_id=user_id,
            title=title,
            description=description,
            priority="high",
            category="compliance_escalation",
        )
        result = await self.tool_executor.execute(
            "ticket_create",
            {
                "client_request_id": f"compliance-escalation:{session_id}",
                "request_payload_hash": request_payload_hash,
                "user_id": user_id,
                "title": title,
                "description": description,
                "priority": "high",
                "category": "compliance_escalation",
            },
            ToolExecutionContext(
                confirmed=True,
                idempotency_key=f"compliance-escalation:{session_id}",
            ),
        )
        if result.success and isinstance(result.result, dict):
            return result.result.get("ticket_id", "")
        return ""

    async def _synthesize(self, state: dict[str, Any]) -> dict[str, Any]:
        if state.get("needs_clarification") and state.get("final_response"):
            final_response = state["final_response"]
        elif not state.get("compliance_passed", True):
            ticket_id = await self._create_escalation_ticket(state)
            base = "抱歉，您的请求涉及敏感内容，已转交人工客服处理。"
            final_response = (
                f"{base}工单编号：{ticket_id}，请留意后续通知。"
                if ticket_id
                else f"{base}工单编号已自动生成，请留意后续通知。"
            )
        else:
            result_parts = []
            for name, result in state.get("sub_results", {}).items():
                if name in {"intent_router", "compliance", "_session_context"}:
                    continue
                if isinstance(result, str) and result:
                    result_parts.append(result)
            final_response = "\n\n".join(result_parts) if result_parts else "抱歉，暂时无法处理您的请求，请稍后重试。"

        messages = list(state.get("messages", []))
        messages.append(AIMessage(content=final_response))
        return {
            **state,
            "final_response": final_response,
            "messages": messages,
        }

    async def ainvoke(
        self, state: dict[str, Any], config: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Invoke one request; config is accepted for caller compatibility only."""
        del config
        if self.checkpoint_store is not None:
            return await self._checkpoint_invoke(state)
        session_id = str(state.get("session_id", "default"))
        user_id = str(state.get("user_id", "anonymous"))
        messages = state.get("messages", [])
        if messages:
            content = getattr(messages[-1], "content", None)
            if isinstance(content, str):
                session_state = (await self.session_store.get_state(session_id)).to_dict()
                await self._admit_incoming_context(session_id, user_id, content, session_state)
        async with self.context_manager.bind_request(
            session_id, user_id, request_id=str(uuid.uuid4()), state=state
        ):
            current = await self._prepare_state(state)
            current = await self._route_intent(current)
            current = await self._handle(current)
            current = await self.compliance_agent.process(current)
            return await self._synthesize(current)

    async def _handle(self, current):
        if not current.get("needs_clarification"):
            handler_name = self._route_name(current.get("intent", "knowledge_rag"))
            if handler_name == "conversation":
                current = await self.conversation_agent.process(current)
            elif handler_name == "knowledge_rag":
                current = await self.knowledge_agent.process(current)
            elif handler_name == "ticket_handler":
                current = await self.ticket_agent.process(current)
            elif handler_name == "refund_handler":
                current = await self.refund_agent.process(current)

        return current

    async def resume(self, session_id: str, user_id: str, client_request_id: str | None = None):
        if self.checkpoint_store is None:
            raise CheckpointConflict("checkpoint recovery is not enabled")
        return await self._checkpoint_invoke({"session_id": session_id, "user_id": user_id,
                                              "client_request_id": client_request_id}, resume=True)

    async def _checkpoint_invoke(self, state, *, resume=False):
        store = self.checkpoint_store
        session_id, user_id = str(state["session_id"]), str(state["user_id"])
        # Validate identifiers before they can enter a query, named lock or snapshot.
        AgentCheckpoint(session_id=session_id, user_id=user_id)
        request_id = state.get("client_request_id") or str(uuid.uuid4())
        if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 128 or "\x00" in request_id:
            raise ValueError("invalid client_request_id")
        async with store.session_lock(session_id):
            cp = await store.load(session_id, user_id)
            if cp is not None:
                _validate_runtime_snapshot(cp)
            if resume:
                if cp is None:
                    raise CheckpointConflict("no checkpoint to resume")
                if not cp.context.get("request_id"):
                    raise CheckpointConflict("session has no chat request to resume")
                if state.get("client_request_id") and cp.context.get("request_id") != request_id:
                    raise CheckpointConflict("requested turn is no longer the active checkpoint")
            else:
                messages = _encode_messages(state.get("messages", []))
                if not messages or messages[-1]["role"] != "user":
                    raise ValueError("a final user message is required")
                request_hash = hashlib.sha256(messages[-1]["content"].encode()).hexdigest()
                receipt = await store.receipt(session_id, user_id, request_id, request_hash)
                if receipt is not None:
                    if cp is not None and cp.context.get("request_id") == request_id and cp.status in {"finished", "waiting"}:
                        await self._process_user_memory(
                            session_id, user_id, request_id, messages[-1]["content"], cp
                        )
                    return receipt
                if cp is not None and cp.status == "running":
                    if cp.context.get("request_id") != request_id or cp.context.get("request_hash") != request_hash:
                        raise CheckpointConflict("unfinished request exists; explicitly resume it before sending a new message")
                else:
                    previous_version = cp.version if cp else 0
                    if cp:
                        session = cp.context["session_state"]
                    else:
                        # MySQL is authoritative. An expired/deleted checkpoint must not revive Redis data.
                        session = ConversationState().to_dict()
                    await self._admit_incoming_context(
                        session_id, user_id, messages[-1]["content"], session
                    )
                    current = {"messages": [HumanMessage(content=messages[-1]["content"])],
                               "user_id": user_id, "session_id": session_id, "intent": "", "sub_results": {},
                               "compliance_passed": True, "final_response": "", "current_agent": "orchestrator", "needs_clarification": False}
                    cp = AgentCheckpoint(session_id=session_id, user_id=user_id, version=previous_version,
                        messages=[messages[-1]], pending_action=session.get("pending_action"),
                        context={"workflow_version": 1, "request_id": request_id, "request_hash": request_hash,
                                 "session_state": session, "state": {k: v for k, v in current.items() if k not in {"messages", "session_id", "user_id"}}})
                    cp = await (store.update(cp) if previous_version else store.save(cp))
            if cp.context.get("workflow_version") != 1:
                raise CheckpointCorrupt("unsupported workflow version; manual migration required")
            try:
                # CheckpointStore.load provides a bounded, trusted compatibility
                # snapshot (at most 20 messages). Full history remains available
                # only through the explicit UI history API; context builds augment
                # this snapshot from their owner-scoped working set.
                current = {**cp.context["state"], "session_id": session_id, "user_id": user_id,
                           "messages": _decode_messages(cp.messages)}
                await self._synchronize_checkpoint(cp, current["messages"])
                session = cp.context["session_state"]
            except (KeyError, TypeError) as exc:
                raise CheckpointCorrupt("checkpoint state is incomplete") from exc
            if cp.status in {"finished", "waiting"}:
                if cp.context.get("request_id"):
                    user_message = next(
                        (item.content for item in reversed(current["messages"]) if isinstance(item, HumanMessage)),
                        "",
                    )
                    await self._process_user_memory(
                        session_id, user_id, cp.context["request_id"], user_message, cp
                    )
                return {**current, "client_request_id": cp.context.get("request_id")}
            if resume:
                request_id = cp.context["request_id"]
            current["_checkpoint_last_event_seq"] = cp.last_event_seq
            current["_checkpoint_version"] = cp.version
            async with self.context_manager.bind_request(
                session_id,
                user_id,
                request_id=request_id,
                state=current,
                last_event_seq=cp.last_event_seq,
                checkpoint_version=cp.version,
            ):
                with self.session_store.checkpoint_context(session_id, session):
                    run = _CheckpointRun(self, cp, current)
                    token = active_checkpoint.set(run)
                    try:
                        if run.checkpoint.current_stage == "PREPARED":
                            current = await self._prepare_state(current)
                            run.state = await self._route_intent(current)
                            await run.save("ROUTED")
                        if run.checkpoint.current_stage == "ROUTED":
                            await run.save("EXECUTING")
                        if run.checkpoint.current_stage == "EXECUTING":
                            run.state = await self._handle(run.state)
                            await run.save("GENERATED")
                        if run.checkpoint.current_stage == "GENERATED":
                            run.state = await self.compliance_agent.process(run.state)
                            await run.save("REVIEWED")
                        if run.checkpoint.current_stage == "REVIEWED":
                            run.state = await self._synthesize(run.state)
                            pending = (await self.session_store.get_state(session_id)).pending_action
                            await run.save("WAIT_CONFIRM" if pending else "FINISHED")
                            user_message = next(
                                (item.content for item in reversed(run.state["messages"]) if isinstance(item, HumanMessage)),
                                "",
                            )
                            await self._process_user_memory(
                                session_id,
                                user_id,
                                run.checkpoint.context["request_id"],
                                user_message,
                                run.checkpoint,
                            )
                        return {**run.state, "client_request_id": run.checkpoint.context["request_id"]}
                    finally:
                        active_checkpoint.reset(token)


def create_chat_orchestrator(
    llm: ChatOpenAI | None = None,
    session_store: SessionStore | None = None,
    long_term_memory: LongTermMemory | None = None,
    mcp_server: MCPToolServer | None = None,
    tool_executor: ToolExecutor | None = None,
    checkpoint_store: CheckpointStore | None = None,
    execution_reconciler=None,
    retriever=None,
    context_manager: ContextManager | None = None,
) -> ChatOrchestrator:
    """Build an orchestrator from the application's already-owned services."""
    if llm is None:
        llm = create_traced_chat_openai(
            model=os.getenv("MODEL_NAME", "deepseek-v4-flash"),
            temperature=float(os.getenv("MODEL_TEMPERATURE", "0")),
        )
    if session_store is None or long_term_memory is None:
        raise ValueError("session_store and long_term_memory are required")
    return ChatOrchestrator(
        llm,
        session_store,
        long_term_memory,
        mcp_server,
        tool_executor,
        checkpoint_store,
        execution_reconciler,
        retriever,
        context_manager,
    )


__all__ = ["ChatOrchestrator", "create_chat_orchestrator"]
