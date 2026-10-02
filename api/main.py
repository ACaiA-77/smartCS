"""
FastAPI入口 — REST API（/api/chat 等）；SSE 流式为规划项
"""

from __future__ import annotations

import logging
import asyncio
import hashlib
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from auth.context import UserContext
from auth.dependency import get_current_user, check_request_origin, cors_allowed_origins
from auth.jwt import issue_token, cookie_options, COOKIE_NAME, TOKEN_TTL_SECONDS
from auth.password import verify_password
from platform_db.database import PlatformDatabase, PlatformUnavailable, PlatformConflict
from platform_db.users import Users
from platform_db.sessions import Sessions

from agents.orchestrator import create_chat_orchestrator
from context.invocation import configure_context_manager
from context.manager import ContextManager, WorkingSetCache
from context.models import (
    ContextCompressionError,
    ContextError,
    ContextOverflowError,
    ContextOwnershipError,
    ModelProfile,
)
from checkpoint.models import AgentCheckpoint, CheckpointError, CheckpointConflict, CheckpointOwnershipError
from checkpoint.store import CheckpointStore
from memory.short_term import ShortTermMemory
from memory.session_store import ConversationState, SessionStore
from memory.knowledge import KnowledgeMemory
from memory.user_memory import UserMemoryService
from memory.user_memory_worker import UserMemoryWorker
from mcp.mcp_server import MCPToolServer, create_default_tools
from mcp.order_repository import OrderRepository
from mcp.approval_store import ApprovalNotFoundError, ApprovalService, ApprovalStateError
from mcp.execution_ledger import ExecutionLedger
from mcp.tool_execution import ToolExecutionContext
from refunds.service import RefundService
from tickets.service import TicketService
from tracing.otel_config import init_tracer, AgentMetrics, set_agent_metrics
from tracing.observability import (
    InstrumentedExecutionReconciler,
    InstrumentedToolExecutor,
    RuntimeMetrics,
    install_request_observability,
)

load_dotenv()

logger = logging.getLogger(__name__)


short_term_memory = ShortTermMemory(
    redis_url=os.getenv("REDIS_URL", "redis://localhost:6379/0"),
    redis_connect_timeout=float(os.getenv("REDIS_CONNECT_TIMEOUT_SECONDS", "0.5")),
    redis_retry_cooldown=float(os.getenv("REDIS_RETRY_COOLDOWN_SECONDS", "30")),
)
session_store = SessionStore(short_term_memory)
long_term_memory = KnowledgeMemory(index_path=os.getenv("FAISS_INDEX_PATH", "./vector_store/faiss_index"))
shared_retriever = long_term_memory.get_retriever()
order_repository = OrderRepository(os.getenv("ORDER_DB_PATH", "./data/orders.db"))
refund_service = RefundService(order_repository)
ticket_service = TicketService(order_repository)
mcp_server = create_default_tools(
    MCPToolServer(),
    long_term_memory=long_term_memory,
    retriever=shared_retriever,
    order_repository=order_repository,
    refund_service=refund_service,
    ticket_service=ticket_service,
)
execution_ledger = ExecutionLedger(order_repository.db_path)
approval_service = ApprovalService(order_repository.db_path)
runtime_metrics = RuntimeMetrics()
execution_reconciler = InstrumentedExecutionReconciler(
    execution_ledger,
    refund_service,
    ticket_service=ticket_service,
    runtime_metrics=runtime_metrics,
)
tool_executor = InstrumentedToolExecutor(
    mcp_server,
    ledger=execution_ledger,
    approval_service=approval_service,
    runtime_metrics=runtime_metrics,
)
metrics = AgentMetrics()
chat_orchestrator = None
checkpoint_store = None
context_manager = None
user_memory_service = None
user_memory_worker = None
WEB_DIR = Path(__file__).resolve().parents[1] / "web"


def _log_rag_runtime() -> None:
    """Log non-sensitive retrieval configuration once during startup."""
    try:
        domains = ",".join(shared_retriever._domains(None)) or "none"
    except Exception:
        domains = "unknown"
    logger.warning(
        "RAG runtime: mode=%s sparse_mode=%s domains=%s embedding=%s reranker=%s",
        "artifact" if shared_retriever.is_artifact_mode else "legacy",
        getattr(shared_retriever, "sparse_mode", "unknown"),
        domains,
        os.getenv("EMBEDDING_MODEL", "default"),
        os.getenv("RAG_RERANKER_BACKEND", "fake"),
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    global chat_orchestrator, checkpoint_store, context_manager, user_memory_service, user_memory_worker

    # Fail startup on invalid budgets before reporting healthy or initializing
    # external services. This validates configuration only; no tokenizer/model
    # provider or weights are loaded here.
    ModelProfile.from_env()

    init_tracer(
        service_name=os.getenv("OTEL_SERVICE_NAME", "smart-cs-multi-agent"),
        otlp_endpoint=os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"),
    )
    set_agent_metrics(metrics)
    checkpoint_store = CheckpointStore.from_env()
    await checkpoint_store.initialize()
    platform = PlatformDatabase.from_env()
    await platform.initialize()
    user_memory_service = UserMemoryService(database=platform)
    await user_memory_service.initialize()
    # Candidate application is owned by this bounded background worker, never
    # by the chat request tail: the durable lease queue survives restarts.
    user_memory_worker = UserMemoryWorker(user_memory_service)
    await user_memory_worker.start()
    context_manager = ContextManager(
        event_store=checkpoint_store,
        cache=WorkingSetCache(short_term_memory, ttl=1800),
        user_memory=user_memory_service,
    )
    configure_context_manager(context_manager)
    app.state.platform_users = Users(platform)
    app.state.platform_sessions = Sessions(platform)
    # Validate JWT configuration at startup; never fall back to a built-in secret.
    issue_token(1)

    summary = execution_reconciler.reconcile_stale()
    logger.info(
        "startup execution recovery: scanned=%d recovered_completed=%d "
        "released_for_retry=%d manual_required=%d skipped=%d",
        summary.get("scanned", 0),
        summary.get("recovered_completed", 0),
        summary.get("released_for_retry", 0),
        summary.get("manual_required", 0),
        summary.get("skipped", 0),
    )

    chat_orchestrator = create_chat_orchestrator(
        session_store=session_store,
        long_term_memory=long_term_memory,
        retriever=shared_retriever,
        mcp_server=mcp_server,
        tool_executor=tool_executor,
        checkpoint_store=checkpoint_store,
        execution_reconciler=execution_reconciler,
        context_manager=context_manager,
    )
    _log_rag_runtime()

    try:
        yield
    finally:
        if user_memory_worker is not None:
            await user_memory_worker.stop()
            user_memory_worker = None
        chat_orchestrator = None
        checkpoint_store = None
        context_manager = None
        user_memory_service = None
        configure_context_manager(None)
        app.state.platform_users = None
        app.state.platform_sessions = None


try:
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    _HAS_FASTAPI_OTEL = True
except ImportError:
    _HAS_FASTAPI_OTEL = False

app = FastAPI(
    title="智能客服多Agent系统",
    description="基于显式 Orchestrator 与 Safe Tool Execution 的智能客服系统",
    version="1.0.0",
    lifespan=lifespan,
)

cors_origins = cors_allowed_origins()
if cors_origins:
    app.add_middleware(CORSMiddleware, allow_origins=cors_origins, allow_credentials=True,
                       allow_methods=["GET", "POST", "DELETE"], allow_headers=["Content-Type", "Authorization"])


@app.middleware("http")
async def private_api_responses(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response

if _HAS_FASTAPI_OTEL:
    FastAPIInstrumentor.instrument_app(app)

@app.exception_handler(ContextError)
async def context_error(_request: Request, exc: ContextError):
    status = 409 if isinstance(exc, (ContextOverflowError, ContextCompressionError, ContextOwnershipError)) else 503
    detail = "context request cannot be assembled" if status == 409 else "context service unavailable"
    return JSONResponse(status_code=status, content={"detail": detail})


@app.exception_handler(CheckpointError)
async def checkpoint_error(_request: Request, exc: CheckpointError):
    code = 403 if isinstance(exc, CheckpointOwnershipError) else 409 if isinstance(exc, CheckpointConflict) else 503
    # These domain exceptions contain no SQL, driver errors or credentials.
    return JSONResponse(status_code=code, content={"detail": str(exc)})


@app.exception_handler(RequestValidationError)
async def invalid_request(_request: Request, exc: RequestValidationError):
    # Pydantic's default error includes raw input, potentially a login password.
    return JSONResponse(status_code=422, content={"detail": [
        {"loc": error["loc"], "type": error["type"], "msg": error["msg"]} for error in exc.errors()
    ]})


@app.exception_handler(PlatformUnavailable)
async def platform_unavailable(_request: Request, _exc: PlatformUnavailable):
    return JSONResponse(status_code=503, content={"detail": "platform database unavailable"})


@app.exception_handler(PlatformConflict)
async def platform_conflict(_request: Request, _exc: PlatformConflict):
    return JSONResponse(status_code=409, content={"detail": "platform identity or ownership conflict"})


# This builds the middleware stack, so register exception handlers first.
install_request_observability(app, runtime_metrics)


@app.get("/", include_in_schema=False)
async def web_workbench() -> FileResponse:
    """Serve the interactive customer-service workbench."""
    return FileResponse(WEB_DIR / "index.html")


app.mount("/ui", StaticFiles(directory=WEB_DIR), name="web-ui")


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: str = Field(min_length=1, max_length=20000)
    session_id: str | None = Field(default=None, min_length=1, max_length=128)
    client_request_id: str | None = Field(default=None, min_length=1, max_length=128)


class ChatResponse(BaseModel):
    response: str
    session_id: str
    intent: str
    compliance_passed: bool
    client_request_id: str | None = None


class ResumeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    client_request_id: str | None = Field(default=None, min_length=1, max_length=128)


class ToolExecuteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    arguments: dict = Field(default_factory=dict)
    confirmed: bool = False
    idempotency_key: str | None = None
    approval_id: str | None = None
    session_id: str | None = None


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=1024, repr=False)


def _public_user(user):
    return {"account_id": int(user["id"]), "username": user["username"]}


@app.post("/api/auth/login")
async def login(body: LoginRequest, request: Request, response: Response):
    check_request_origin(request)
    users = getattr(app.state, "platform_users", None)
    if users is None:
        raise HTTPException(503, "authentication unavailable")
    user = await users.by_username(body.username)
    # The shared verifier does equal password work for unknown accounts.
    valid = await asyncio.to_thread(verify_password, body.password, user["password_hash"] if user else None)
    if not valid or user is None or user["status"] != "active":
        raise HTTPException(401, "invalid credentials")
    response.set_cookie(COOKIE_NAME, issue_token(user["id"]), max_age=TOKEN_TTL_SECONDS, **cookie_options())
    response.headers["Cache-Control"] = "no-store"
    return {"user": _public_user(user)}


@app.post("/api/auth/logout")
async def logout(request: Request, response: Response):
    check_request_origin(request)
    response.delete_cookie(COOKIE_NAME, **cookie_options())
    response.headers["Cache-Control"] = "no-store"
    return {"logged_out": True}


@app.get("/api/auth/me")
async def me(response: Response, user: UserContext = Depends(get_current_user)):
    response.headers["Cache-Control"] = "no-store"
    return {"account_id": user.account_id, "username": user.username}


async def _owned_session(session_id: str, user: UserContext):
    sessions = getattr(app.state, "platform_sessions", None)
    if sessions is None:
        raise HTTPException(503, "session database unavailable")
    session = await sessions.get_owned(session_id, user.account_id)
    if session is None:
        raise HTTPException(404, "session not found")
    return session


@app.get("/api/sessions")
async def list_sessions(user: UserContext = Depends(get_current_user)):
    return {"sessions": await app.state.platform_sessions.list_owned(user.account_id)}


@app.post("/api/sessions")
async def create_session(user: UserContext = Depends(get_current_user)):
    return await app.state.platform_sessions.create(user.account_id)


@app.get("/api/sessions/{session_id}")
async def session_detail(session_id: str, user: UserContext = Depends(get_current_user)):
    session = await _owned_session(session_id, user)
    history = await get_history(session_id, user)
    return {**session, "messages": history["messages"]}


@app.delete("/api/sessions/{session_id}")
async def delete_session(session_id: str, user: UserContext = Depends(get_current_user)):
    await clear_history(session_id, user)
    await app.state.platform_sessions.delete(session_id, user.account_id)
    return {"session_id": session_id, "deleted": True}


class ApprovalCreateRequest(BaseModel):
    tool_name: str
    arguments: dict = Field(default_factory=dict)
    requested_by: str | None = None


class ApprovalDecisionRequest(BaseModel):
    decided_by: str
    reason: str = ""


def _customer_arguments(name, arguments, user):
    # Customers cannot invoke arbitrary registered tools or self-authorize writes.
    if name not in {"order_query", "refund_evaluate", "ticket_query", "knowledge_search"}:
        raise HTTPException(403, "use customer chat for this operation")
    if "user_id" in arguments and arguments["user_id"] != user.business_user_id:
        raise HTTPException(403, "tool identity mismatch")
    if "business_user_id" in arguments:
        raise HTTPException(403, "tool identity must come from authentication")
    result = dict(arguments)
    if name != "knowledge_search":
        result["user_id"] = user.business_user_id
    else:
        result.pop("user_id", None)
    return result


async def _internal_only(user: UserContext = Depends(get_current_user)):
    # No admin role exists in this round. Internal Python services remain available.
    raise HTTPException(403, "internal operation is not exposed to customers")


async def _persist_order_query_context(
    session_id: str | None,
    user_id: str,
    order: dict,
    *,
    tool_arguments: dict | None = None,
    tool_result: dict | None = None,
    event_operation_id: str | None = None,
) -> None:
    """把网页端直接调用订单工具的结果接入同一会话记忆。"""
    if not session_id or not order.get("found") or not order.get("order_id"):
        return

    order_id = str(order["order_id"])
    if checkpoint_store is not None:
        async with checkpoint_store.session_lock(session_id):
            cp = await checkpoint_store.load(session_id, user_id)
            if cp is not None and cp.status == "running":
                raise CheckpointConflict("unfinished chat request owns this session")
            context = dict(cp.context) if cp else {
                "workflow_version": 1,
                "session_state": ConversationState().to_dict(),
                "state": {},
            }
            current = ConversationState.from_dict(context.get("session_state", {}))
            current.last_intent = "order_query"
            current.accumulated_entities["order_id"] = order_id
            context["session_state"] = current.to_dict()

            operation_id = event_operation_id if isinstance(event_operation_id, str) and event_operation_id else str(uuid.uuid4())
            operation_hash = hashlib.sha256(
                f"{session_id}:{user_id}:{operation_id}".encode("utf-8")
            ).hexdigest()[:40]
            event_key = f"direct-order-query:{operation_hash}"
            safe_arguments = {"order_id": order_id}
            result_payload = dict(tool_result or order)
            for private_key in ("user_id", "business_user_id", "customer_email", "phone"):
                result_payload.pop(private_key, None)
            await checkpoint_store.append_event(
                session_id,
                user_id,
                "TOOL_CALL",
                {"name": "order_query", "arguments": tool_arguments or safe_arguments},
                event_key=f"{event_key}:call",
            )
            await checkpoint_store.append_event(
                session_id,
                user_id,
                "TOOL_RESULT",
                {"name": "order_query", "success": True, "result": result_payload},
                event_key=f"{event_key}:result",
            )
            context_message = (
                f"上一轮已查询订单 {order_id}，状态：{order.get('status_label', order.get('status', '未知'))}，"
                f"商品：{order.get('product', '未知')}。"
            )
            synthetic_user_message = f"查询订单 {order_id}"
            await checkpoint_store.append_event(
                session_id,
                user_id,
                "USER_MESSAGE",
                {"role": "user", "content": synthetic_user_message, "synthetic": True},
                event_key=f"{event_key}:user",
            )
            await checkpoint_store.append_event(
                session_id,
                user_id,
                "ASSISTANT_MESSAGE",
                {"role": "assistant", "content": context_message},
                event_key=f"{event_key}:assistant",
            )
            # These context events do not belong to the previous chat receipt.
            context.pop("request_id", None)
            context.pop("request_hash", None)
            value = AgentCheckpoint(
                session_id=session_id,
                user_id=user_id,
                intent="order_query",
                current_stage="WAIT_CONFIRM" if current.pending_action else "FINISHED",
                status="waiting" if current.pending_action else "finished",
                pending_action=current.pending_action,
                messages=[],
                context=context,
                version=cp.version if cp else 0,
                last_event_seq=cp.last_event_seq if cp else 0,
            )
            saved = await (checkpoint_store.update(value) if cp else checkpoint_store.save(value))
            if context_manager is not None:
                synchronize = getattr(context_manager, "synchronize_checkpoint", None)
                if callable(synchronize):
                    projection = AgentCheckpoint.model_validate({
                        **saved.model_dump(),
                        "messages": [
                            {"role": "user", "content": synthetic_user_message},
                            {"role": "assistant", "content": context_message},
                        ],
                    })
                    await synchronize(projection)
        return
    current = await session_store.get_state(session_id)
    entities = dict(current.accumulated_entities or {})
    entities["order_id"] = order_id
    await session_store.update_state(
        session_id,
        last_intent="order_query",
        accumulated_entities=entities,
    )

    context_message = (
        f"上一轮已查询订单 {order_id}，状态：{order.get('status_label', order.get('status', '未知'))}，"
        f"商品：{order.get('product', '未知')}。"
    )
    await session_store.add_message(session_id, "user", f"查询订单 {order_id}")
    await session_store.add_message(session_id, "assistant", context_message)


@app.post("/api/chat", response_model=ChatResponse)
async def chat(request: ChatRequest, user: UserContext = Depends(get_current_user)):
    """主聊天接口"""
    if chat_orchestrator is None:
        raise HTTPException(status_code=503, detail="系统初始化中")

    if request.session_id:
        session_id = request.session_id
        await _owned_session(session_id, user)
    else:
        session = await app.state.platform_sessions.create(user.account_id, title=request.message[:100],
                                                          client_request_id=request.client_request_id)
        session_id = session["session_id"]
    await app.state.platform_sessions.touch(session_id, user.account_id, title=request.message[:100])

    if checkpoint_store is not None:
        from langchain_core.messages import HumanMessage
        try:
            result = await chat_orchestrator.ainvoke({
                "session_id": session_id, "user_id": user.business_user_id,
                "client_request_id": request.client_request_id,
                "messages": [HumanMessage(content=request.message)],
            })
        except (CheckpointError, ContextError):
            raise
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid checkpoint request") from exc
        except Exception as exc:
            raise HTTPException(status_code=503, detail="请求中断，可查询 checkpoint 后显式恢复") from exc
        return _chat_response(session_id, result)

    await session_store.add_message(session_id, "user", request.message)

    from langchain_core.messages import HumanMessage, AIMessage

    history = await session_store.get_history(session_id)
    messages: list = []
    for msg in history:
        if msg["role"] == "user":
            messages.append(HumanMessage(content=msg["content"]))
        elif msg["role"] == "assistant":
            messages.append(AIMessage(content=msg["content"]))

    if not messages:
        messages = [HumanMessage(content=request.message)]

    initial_state = {
        "messages": messages,
        "user_id": user.business_user_id,
        "session_id": session_id,
        "intent": "",
        "sub_results": {},
        "compliance_passed": True,
        "final_response": "",
        "current_agent": "",
        "retry_count": 0,
        "needs_clarification": False,
    }

    try:
        result = await chat_orchestrator.ainvoke(initial_state)
    except ContextError:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"处理失败: {str(e)}")

    final_response = result.get("final_response", "系统处理异常，请稍后重试")

    await session_store.add_message(session_id, "assistant", final_response)

    return ChatResponse(
        response=final_response,
        session_id=session_id,
        intent=result.get("intent", "unknown"),
        compliance_passed=result.get("compliance_passed", True),
    )


def _chat_response(session_id, result):
    return ChatResponse(response=result["final_response"], session_id=session_id,
                        intent=result.get("intent", "unknown"), compliance_passed=result.get("compliance_passed", True),
                        client_request_id=result.get("client_request_id"))


@app.get("/api/checkpoints/{session_id}")
async def get_checkpoint(session_id: str, user: UserContext = Depends(get_current_user)):
    await _owned_session(session_id, user)
    if checkpoint_store is None:
        raise HTTPException(status_code=503, detail="checkpoint unavailable")
    cp = await checkpoint_store.load(session_id, user.business_user_id)
    if cp is None:
        raise HTTPException(status_code=404, detail="checkpoint not found")
    return {"session_id": session_id, "client_request_id": cp.context.get("request_id"),
            "current_stage": cp.current_stage, "status": cp.status, "version": cp.version,
            "response": cp.context.get("state", {}).get("final_response") if cp.status != "running" else None}


@app.post("/api/checkpoints/{session_id}/resume", response_model=ChatResponse)
async def resume_checkpoint(session_id: str, request: ResumeRequest, user: UserContext = Depends(get_current_user)):
    await _owned_session(session_id, user)
    if checkpoint_store is None or chat_orchestrator is None:
        raise HTTPException(status_code=503, detail="checkpoint unavailable")
    try:
        result = await chat_orchestrator.resume(session_id, user.business_user_id, request.client_request_id)
    except (CheckpointError, ContextError):
        raise
    except Exception as exc:
        raise HTTPException(status_code=503, detail="恢复未完成，已保存断点；请检查服务或业务执行账本") from exc
    return _chat_response(session_id, result)


@app.get("/api/history/{session_id}")
async def get_history(session_id: str, user: UserContext = Depends(get_current_user)):
    """获取对话历史"""
    await _owned_session(session_id, user)
    if checkpoint_store is not None:
        history = await checkpoint_store.history(session_id, user.business_user_id)
    else:
        history = await session_store.get_history(session_id)
    return {"session_id": session_id, "messages": history}


@app.delete("/api/history/{session_id}")
async def clear_history(session_id: str, user: UserContext = Depends(get_current_user)):
    """清除会话消息和结构化状态。"""
    await _owned_session(session_id, user)
    user_id = user.business_user_id
    if checkpoint_store is not None:
        async with checkpoint_store.session_lock(session_id):
            cp = await checkpoint_store.load(session_id, user_id)
            if cp is not None and cp.status == "running":
                raise CheckpointConflict("unfinished request cannot be deleted")
            await session_store.clear(session_id)
            if cp is not None:
                await checkpoint_store.delete(session_id, user_id, cp.version)
            if context_manager is not None:
                await context_manager.invalidate_session(session_id, user_id)
    else:
        await session_store.clear(session_id)
    return {"session_id": session_id, "cleared": True}


@app.get("/api/demo/orders")
async def list_demo_orders(limit: int = Query(default=6, ge=1, le=20), user: UserContext = Depends(get_current_user)):
    """Expose recent local demo orders for the web workbench quick actions."""
    return {
        "data_source": order_repository.DEMO_DATA_SOURCE,
        "orders": order_repository.list_orders_for_user(user.business_user_id, limit),
    }


@app.get("/api/tools")
async def list_tools(user: UserContext = Depends(get_current_user)):
    """MCP工具发现接口"""
    return {"tools": mcp_server.list_tools()}


@app.post("/api/tools/call")
async def call_tool(request: ToolExecuteRequest, user: UserContext = Depends(get_current_user)):
    """MCP工具调用接口"""
    name = request.name
    arguments = _customer_arguments(name, request.arguments, user)
    if request.session_id:
        await _owned_session(request.session_id, user)
    tool = mcp_server.get_tool(name)
    if tool is not None and str(tool.operation_type or "read").lower() == "write":
        raise HTTPException(
            status_code=409,
            detail="write tools must use /api/tools/execute",
        )
    result = await mcp_server.call_tool(
        name=name,
        arguments=arguments,
    )
    if name == "order_query" and result.success and isinstance(result.result, dict):
        await _persist_order_query_context(
            session_id=request.session_id,
            user_id=user.business_user_id,
            order=result.result,
            tool_arguments={"order_id": arguments.get("order_id")},
            tool_result=result.result,
            event_operation_id=request.idempotency_key,
        )
    return {
        "success": result.success,
        "result": result.result,
        "error": result.error,
        "duration_ms": result.duration_ms,
    }


@app.post("/api/tools/execute")
async def execute_tool(request: ToolExecuteRequest, user: UserContext = Depends(get_current_user)):
    """通过统一执行层调用 MCP 工具。"""
    arguments = _customer_arguments(request.name, request.arguments, user)
    if request.session_id:
        await _owned_session(request.session_id, user)
    try:
        result = await tool_executor.execute(
            request.name,
            arguments,
            ToolExecutionContext(
                confirmed=request.confirmed,
                idempotency_key=request.idempotency_key,
                approval_id=request.approval_id,
            ),
        )
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    if (
        request.name == "order_query"
        and result.success
        and isinstance(result.result, dict)
    ):
        await _persist_order_query_context(
            session_id=request.session_id,
            user_id=user.business_user_id,
            order=result.result,
            tool_arguments={"order_id": arguments.get("order_id")},
            tool_result=result.result,
            event_operation_id=request.idempotency_key,
        )
    return result.as_dict()


@app.post("/api/approvals", dependencies=[Depends(_internal_only)])
async def create_approval(request: ApprovalCreateRequest):
    """创建本地 Sandbox 人工审批记录。"""
    try:
        record = approval_service.create_request(
            request.tool_name,
            request.arguments,
            request.requested_by,
        )
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return record.as_dict()


@app.get("/api/approvals/{approval_id}", dependencies=[Depends(_internal_only)])
async def get_approval(approval_id: str):
    record = approval_service.get(approval_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"approval not found: {approval_id}")
    return record.as_dict()


@app.post("/api/approvals/{approval_id}/approve", dependencies=[Depends(_internal_only)])
async def approve_approval(approval_id: str, request: ApprovalDecisionRequest):
    try:
        record = approval_service.approve(
            approval_id,
            request.decided_by,
            request.reason,
        )
    except ApprovalNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ApprovalStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return record.as_dict()


@app.post("/api/approvals/{approval_id}/reject", dependencies=[Depends(_internal_only)])
async def reject_approval(approval_id: str, request: ApprovalDecisionRequest):
    try:
        record = approval_service.reject(
            approval_id,
            request.decided_by,
            request.reason,
        )
    except ApprovalNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ApprovalStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return record.as_dict()


@app.get("/api/metrics", dependencies=[Depends(_internal_only)])
async def get_metrics():
    """获取系统指标"""
    return {
        "agent_metrics": metrics.get_summary(),
        "tool_call_log": mcp_server.get_call_log(last_n=20),
    }


@app.get("/api/metrics/runtime")
async def get_runtime_metrics(user: UserContext = Depends(get_current_user)):
    """获取经认证的进程级数值运行时指标，不包含会话内容。"""
    snapshot = runtime_metrics.snapshot()
    context_snapshot = getattr(context_manager, "metrics_snapshot", None)
    snapshot["context"] = context_snapshot() if callable(context_snapshot) else {}
    return snapshot


@app.get("/health")
async def health_check():
    return {"status": "healthy", "version": "1.0.0"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "api.main:app",
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8000")),
        reload=True,
    )
