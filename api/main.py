"""FastAPI Business Runtime entrypoint (REST + SSE streaming forwarded to pi-harness)."""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from auth.context import UserContext
from auth.dependency import check_request_origin, cors_allowed_origins, get_current_user
from auth.jwt import COOKIE_NAME, TOKEN_TTL_SECONDS, cookie_options, issue_token
from auth.password import verify_password
from api.copilot_bootstrap import build_copilot_dependencies, is_copilot_profile
from api.feishu_bot import configure_feishu_client, router as feishu_router
from internal_api import internal_router
from internal_api.harness_client import (
    HarnessUnavailable,
    call_harness,
    forward_chat,
    harness_version_for_account,
    rollout_percent,
    stream_chat,
)
from memory.knowledge import KnowledgeMemory
from memory.user_memory import UserMemoryService
from memory.user_memory_worker import UserMemoryWorker
from platform_db.database import PlatformConflict, PlatformDatabase, PlatformUnavailable
from platform_db.sessions import Sessions
from platform_db.users import Users
from refunds.service import RefundService
from tickets.service import TicketService
from tool_runtime.approval_store import ApprovalNotFoundError, ApprovalService, ApprovalStateError
from tool_runtime.execution_ledger import ExecutionLedger
from tool_runtime.mcp_server import MCPToolServer, create_default_tools
from tool_runtime.order_repository import OrderRepository
from tool_runtime.tool_execution import ToolExecutionContext
from tracing.observability import (
    InstrumentedExecutionReconciler,
    InstrumentedToolExecutor,
    RuntimeMetrics,
    install_request_observability,
)
from tracing.otel_config import AgentMetrics, init_tracer, set_agent_metrics

load_dotenv()

logger = logging.getLogger(__name__)

long_term_memory = KnowledgeMemory(index_path=os.getenv("FAISS_INDEX_PATH", "./vector_store/faiss_index"))
shared_retriever = long_term_memory.get_retriever()
order_repository = OrderRepository(os.getenv("ORDER_DB_PATH", "./data/orders.db"))
refund_service = RefundService(order_repository)
ticket_service = TicketService(order_repository)
# Copilot profile only: build connectors whose environment is complete. The
# default e-commerce deployment passes no extra dependencies, so its tool
# surface is byte-for-byte unchanged.
copilot_dependencies = build_copilot_dependencies() if is_copilot_profile() else {}
mcp_server = create_default_tools(
    MCPToolServer(),
    long_term_memory=long_term_memory,
    retriever=shared_retriever,
    order_repository=order_repository,
    refund_service=refund_service,
    ticket_service=ticket_service,
    **copilot_dependencies,
)
if is_copilot_profile():
    configure_feishu_client(copilot_dependencies.get("feishu_client"))
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
    """Application lifecycle management."""
    global user_memory_service, user_memory_worker

    init_tracer(
        service_name=os.getenv("OTEL_SERVICE_NAME", "smart-cs-multi-agent"),
        otlp_endpoint=os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"),
    )
    set_agent_metrics(metrics)
    platform = PlatformDatabase.from_env()
    await platform.initialize()
    user_memory_service = UserMemoryService(database=platform)
    await user_memory_service.initialize()
    user_memory_worker = UserMemoryWorker(user_memory_service)
    await user_memory_worker.start()

    app.state.platform_users = Users(platform)
    app.state.platform_sessions = Sessions(platform)
    app.state.platform_database = platform
    app.state.execution_ledger = execution_ledger
    app.state.tool_executor = tool_executor
    app.state.order_repository = order_repository
    app.state.user_memory_service = user_memory_service

    issue_token(1)
    logger.info("pi cohort rollout: percent=%d", rollout_percent())

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
    _log_rag_runtime()

    try:
        yield
    finally:
        if user_memory_worker is not None:
            await user_memory_worker.stop()
            user_memory_worker = None
        user_memory_service = None
        app.state.platform_users = None
        app.state.platform_sessions = None
        app.state.tool_executor = None
        app.state.platform_database = None
        app.state.execution_ledger = None
        app.state.order_repository = None
        app.state.user_memory_service = None


try:
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

    _HAS_FASTAPI_OTEL = True
except ImportError:
    _HAS_FASTAPI_OTEL = False

app = FastAPI(
    title="智能客服系统",
    description="基于 Pi Harness 与 Safe Tool Execution 的双层智能客服系统",
    version="1.0.0",
    lifespan=lifespan,
)

for _handle in (
    "platform_users",
    "platform_sessions",
    "platform_database",
    "execution_ledger",
    "tool_executor",
    "order_repository",
    "user_memory_service",
):
    setattr(app.state, _handle, None)
del _handle

cors_origins = cors_allowed_origins()
if cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "DELETE"],
        allow_headers=["Content-Type", "Authorization"],
    )


@app.middleware("http")
async def private_api_responses(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


app.include_router(internal_router)
app.include_router(feishu_router)

if _HAS_FASTAPI_OTEL:
    FastAPIInstrumentor.instrument_app(app)


@app.exception_handler(RequestValidationError)
async def invalid_request(_request: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=422,
        content={"detail": [{"loc": error["loc"], "type": error["type"], "msg": error["msg"]} for error in exc.errors()]},
    )


@app.exception_handler(PlatformUnavailable)
async def platform_unavailable(_request: Request, _exc: PlatformUnavailable):
    return JSONResponse(status_code=503, content={"detail": "platform database unavailable"})


@app.exception_handler(PlatformConflict)
async def platform_conflict(_request: Request, _exc: PlatformConflict):
    return JSONResponse(status_code=409, content={"detail": "platform identity or ownership conflict"})


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
    harness_version: str = "pi"


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


async def _create_session(user: UserContext, *, title: str = "", client_request_id: str | None = None) -> dict:
    session = await app.state.platform_sessions.create(
        user.account_id,
        title=title,
        client_request_id=client_request_id,
        harness_version=harness_version_for_account(user.account_id),
    )
    logger.info(
        "session created: session_id=%s harness_version=%s",
        session["session_id"],
        session["harness_version"],
    )
    return session


@app.post("/api/sessions")
async def create_session(user: UserContext = Depends(get_current_user)):
    return await _create_session(user)


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
    raise HTTPException(403, "internal operation is not exposed to customers")


async def _forward_user_token(request: Request) -> str:
    cookie = request.cookies.get(COOKIE_NAME)
    authorization = request.headers.get("authorization")
    if authorization is not None:
        parts = authorization.split()
        if len(parts) == 2 and parts[0].lower() == "bearer":
            return parts[1]
    return cookie or ""


async def _resolve_pi_session(body: ChatRequest, user: UserContext) -> str:
    if body.session_id:
        session_id = body.session_id
        session = await _owned_session(session_id, user)
    else:
        session = await _create_session(user, title=body.message[:100], client_request_id=body.client_request_id)
        session_id = session["session_id"]
    await app.state.platform_sessions.touch(session_id, user.account_id, title=body.message[:100])
    if (session.get("harness_version") or "legacy") != "pi":
        raise HTTPException(status_code=409, detail="legacy orchestrator is retired; create a new session")
    return session_id


@app.post("/api/chat", response_model=ChatResponse)
async def chat(
    body: ChatRequest,
    user: UserContext = Depends(get_current_user),
    user_token: str = Depends(_forward_user_token),
):
    """Unified chat entry forwarded to pi-harness."""
    session_id = await _resolve_pi_session(body, user)
    return await _pi_chat(user_token, body, session_id, user)


@app.post("/api/chat/stream")
async def chat_stream(
    body: ChatRequest,
    user: UserContext = Depends(get_current_user),
    user_token: str = Depends(_forward_user_token),
):
    """Unified SSE streaming chat entry forwarded to pi-harness (/api/chat/stream)."""
    session_id = await _resolve_pi_session(body, user)
    client_request_id = body.client_request_id or str(uuid.uuid4())
    return StreamingResponse(
        stream_chat(
            account_id=user.account_id,
            business_user_id=user.business_user_id,
            session_id=session_id,
            client_request_id=client_request_id,
            message=body.message,
            user_token=user_token,
        ),
        media_type="text/event-stream; charset=utf-8",
        headers={
            "Cache-Control": "no-store, no-transform",
            "Connection": "keep-alive",
            "X-SmartCS-Session-Id": session_id,
            "X-SmartCS-Client-Request-Id": client_request_id,
        },
    )


def _harness_detail(response, fallback: str) -> str:
    try:
        detail = response.json().get("detail")
    except ValueError:
        detail = None
    if isinstance(detail, str) and 0 < len(detail) <= 200:
        return detail
    return fallback


async def _pi_chat(user_token: str, body: ChatRequest, session_id: str, user: UserContext) -> ChatResponse:
    client_request_id = body.client_request_id or str(uuid.uuid4())
    try:
        response = await forward_chat(
            account_id=user.account_id,
            business_user_id=user.business_user_id,
            session_id=session_id,
            client_request_id=client_request_id,
            message=body.message,
            user_token=user_token,
        )
    except HarnessUnavailable:
        raise HTTPException(503, "pi harness unavailable") from None

    if response.status_code in (409, 429):
        raise HTTPException(response.status_code, _harness_detail(response, "pi harness refused the request"))
    if response.status_code != 200:
        raise HTTPException(502, "pi harness rejected the request")
    try:
        payload = response.json()
    except ValueError:
        raise HTTPException(502, "pi harness returned an invalid response") from None
    return _pi_chat_response(session_id, client_request_id, payload)


def _pi_chat_response(session_id: str, client_request_id: str, payload) -> ChatResponse:
    message = payload.get("message") if isinstance(payload, dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str) or not content:
        raise HTTPException(502, "pi harness returned an invalid response")
    intent = payload.get("intent_label")
    returned_request_id = payload.get("client_request_id")
    return ChatResponse(
        response=content,
        session_id=session_id,
        intent=intent if isinstance(intent, str) and intent else "unknown",
        compliance_passed=True,
        client_request_id=returned_request_id if isinstance(returned_request_id, str) else client_request_id,
        harness_version="pi",
    )


async def _pi_harness_call(method: str, session_id: str, user: UserContext) -> dict:
    try:
        response = await call_harness(
            method,
            f"/internal/history/{session_id}",
            account_id=user.account_id,
            business_user_id=user.business_user_id,
            session_id=session_id,
        )
    except HarnessUnavailable:
        raise HTTPException(503, "pi harness unavailable") from None
    if response.status_code == 409:
        raise HTTPException(409, "unfinished request cannot be deleted")
    if response.status_code == 404:
        raise HTTPException(404, "session not found")
    if response.status_code != 200:
        raise HTTPException(502, "pi harness rejected the request")
    return response.json()


@app.get("/api/history/{session_id}")
async def get_history(session_id: str, user: UserContext = Depends(get_current_user)):
    session = await _owned_session(session_id, user)
    if (session.get("harness_version") or "legacy") != "pi":
        raise HTTPException(409, "legacy orchestrator is retired; create a new session")
    payload = await _pi_harness_call("GET", session_id, user)
    return {"session_id": session_id, "messages": payload.get("messages", [])}


@app.delete("/api/history/{session_id}")
async def clear_history(session_id: str, user: UserContext = Depends(get_current_user)):
    session = await _owned_session(session_id, user)
    if (session.get("harness_version") or "legacy") != "pi":
        raise HTTPException(409, "legacy orchestrator is retired; create a new session")
    await _pi_harness_call("DELETE", session_id, user)
    return {"session_id": session_id, "cleared": True}


@app.get("/api/demo/orders")
async def list_demo_orders(limit: int = Query(default=6, ge=1, le=20), user: UserContext = Depends(get_current_user)):
    return {
        "data_source": order_repository.DEMO_DATA_SOURCE,
        "orders": order_repository.list_orders_for_user(user.business_user_id, limit),
    }


@app.get("/api/tools")
async def list_tools(user: UserContext = Depends(get_current_user)):
    return {"tools": mcp_server.list_tools()}


@app.post("/api/tools/call")
async def call_tool(request: ToolExecuteRequest, user: UserContext = Depends(get_current_user)):
    name = request.name
    arguments = _customer_arguments(name, request.arguments, user)
    if request.session_id:
        await _owned_session(request.session_id, user)
    tool = mcp_server.get_tool(name)
    if tool is not None and str(tool.operation_type or "read").lower() == "write":
        raise HTTPException(status_code=409, detail="write tools must use /api/tools/execute")
    result = await mcp_server.call_tool(name=name, arguments=arguments)
    return {
        "success": result.success,
        "result": result.result,
        "error": result.error,
        "duration_ms": result.duration_ms,
    }


@app.post("/api/tools/execute")
async def execute_tool(request: ToolExecuteRequest, user: UserContext = Depends(get_current_user)):
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
    return result.as_dict()


@app.post("/api/approvals", dependencies=[Depends(_internal_only)])
async def create_approval(request: ApprovalCreateRequest):
    try:
        record = approval_service.create_request(request.tool_name, request.arguments, request.requested_by)
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
        record = approval_service.approve(approval_id, request.decided_by, request.reason)
    except ApprovalNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ApprovalStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return record.as_dict()


@app.post("/api/approvals/{approval_id}/reject", dependencies=[Depends(_internal_only)])
async def reject_approval(approval_id: str, request: ApprovalDecisionRequest):
    try:
        record = approval_service.reject(approval_id, request.decided_by, request.reason)
    except ApprovalNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ApprovalStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return record.as_dict()


@app.get("/api/metrics", dependencies=[Depends(_internal_only)])
async def get_metrics():
    return {
        "agent_metrics": metrics.get_summary(),
        "tool_call_log": mcp_server.get_call_log(last_n=20),
    }


@app.get("/api/metrics/runtime")
async def get_runtime_metrics(user: UserContext = Depends(get_current_user)):
    return runtime_metrics.snapshot()


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
