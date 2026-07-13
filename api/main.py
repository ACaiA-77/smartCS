"""FastAPI entrypoint for the SmartCS REST API."""

from __future__ import annotations

import logging
import os
import uuid
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from agents.supervisor import create_supervisor_graph
from api.health import build_readiness_report
from api.schemas import ChatRequest, ChatResponse, SafeIdentifier, ToolCallRequest
from api.settings import AppSettings
from memory.long_term import LongTermMemory
from memory.short_term import ShortTermMemory
from memory.working_memory import WorkingMemory
from mcp.mcp_server import MCPToolServer, create_default_tools
from tracing.otel_config import AgentMetrics, init_tracer, set_agent_metrics

load_dotenv()

logger = logging.getLogger(__name__)
settings = AppSettings.from_env()

working_memory = WorkingMemory()
short_term_memory = ShortTermMemory(
    redis_url=os.getenv("REDIS_URL", "redis://localhost:6379/0"),
    redis_unavailable_retry_seconds=settings.redis_unavailable_retry_seconds,
)
long_term_memory = LongTermMemory(
    index_path=os.getenv("FAISS_INDEX_PATH", "./vector_store/faiss_index"),
    min_score=settings.rag_min_score,
)
mcp_server = create_default_tools(MCPToolServer())
metrics = AgentMetrics()
graph = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize runtime dependencies and release connections on shutdown."""
    global graph

    init_tracer(
        service_name=os.getenv("OTEL_SERVICE_NAME", "smart-cs-multi-agent"),
        otlp_endpoint=os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"),
    )
    set_agent_metrics(metrics)
    graph = create_supervisor_graph(
        working_memory=working_memory,
        short_term_memory=short_term_memory,
        long_term_memory=long_term_memory,
        mcp_server=mcp_server,
        intent_confidence_threshold=settings.intent_confidence_threshold,
        intent_candidate_margin=settings.intent_candidate_margin,
        intent_context_turns=settings.intent_context_turns,
        intent_entity_ttl_turns=settings.intent_entity_ttl_turns,
        intent_format_repair_enabled=settings.intent_format_repair_enabled,
        intent_prompt_version=settings.intent_prompt_version,
        rag_query_rewrite_enabled=settings.rag_query_rewrite_enabled,
        rag_llm_rerank_enabled=settings.rag_llm_rerank_enabled,
        compliance_llm_review_enabled=settings.compliance_llm_review_enabled,
    )

    try:
        yield
    finally:
        graph = None
        await short_term_memory.close()


try:
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

    _HAS_FASTAPI_OTEL = True
except ImportError:
    _HAS_FASTAPI_OTEL = False

app = FastAPI(
    title="智能客服多Agent系统",
    description="基于LangGraph的Supervisor编排多Agent智能客服系统",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=list(settings.cors_allowed_origins),
    allow_credentials=settings.cors_allow_credentials,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
)

if _HAS_FASTAPI_OTEL:
    FastAPIInstrumentor.instrument_app(app)


@app.post("/api/chat", response_model=ChatResponse)
async def chat(request: ChatRequest):
    """Process one user chat message."""
    if graph is None:
        raise HTTPException(status_code=503, detail="系统初始化中")

    session_id = request.session_id or str(uuid.uuid4())
    await short_term_memory.add_message(session_id, "user", request.message)

    from langchain_core.messages import AIMessage, HumanMessage

    history = await short_term_memory.get_history(session_id)
    messages: list = []
    for message in history:
        if message["role"] == "user":
            messages.append(HumanMessage(content=message["content"]))
        elif message["role"] == "assistant":
            messages.append(AIMessage(content=message["content"]))

    if not messages:
        messages = [HumanMessage(content=request.message)]

    initial_state = {
        "messages": messages,
        "user_id": request.user_id,
        "session_id": session_id,
        "intent": "",
        "sub_results": {},
        "compliance_passed": True,
        "final_response": "",
        "current_agent": "",
        "retry_count": 0,
        "needs_clarification": False,
        "response_mode": "execution",
    }
    config = {"configurable": {"thread_id": session_id}}
    request_id = uuid.uuid4().hex

    try:
        result = await graph.ainvoke(initial_state, config=config)
    except Exception as exc:
        logger.error(
            "chat processing failed request_id=%s error_type=%s",
            request_id,
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=500,
            detail={"code": "CHAT_PROCESSING_FAILED", "request_id": request_id},
        ) from exc

    final_response = result.get("final_response", "系统处理异常，请稍后重试")

    # Persist internal state separately from user/assistant messages in a later
    # state-store migration. It remains a system entry for backward compatibility.
    wm_export = working_memory.export_for_persistence(session_id)
    wm_ctx = wm_export.get("context", {})
    if wm_ctx:
        import json as _json

        persist_data = {
            "last_intent": wm_ctx.get("last_intent"),
            "accumulated_entities": wm_ctx.get("accumulated_entities", {}),
            "turn_count": wm_ctx.get("turn_count", 0),
        }
        await short_term_memory.add_message(
            session_id,
            "system",
            f"[wm_snapshot]{_json.dumps(persist_data, ensure_ascii=False)}",
        )

    await short_term_memory.add_message(session_id, "assistant", final_response)

    route_result = result.get("sub_results", {}).get("intent_router", {})
    secondary_intent = str(route_result.get("secondary", "unknown"))
    needs_clarification = bool(result.get("needs_clarification", False))
    response_mode = str(
        result.get("response_mode")
        or ("clarification" if needs_clarification else "security_guidance" if result.get("intent") == "compliance_checker" else "execution")
    )
    return ChatResponse(
        response=final_response,
        session_id=session_id,
        intent=result.get("intent", "unknown"),
        secondary_intent=secondary_intent,
        response_mode=response_mode,
        needs_clarification=needs_clarification,
        compliance_passed=result.get("compliance_passed", True),
    )


@app.get("/api/history/{session_id}")
async def get_history(session_id: SafeIdentifier):
    """Return the bounded conversation history for a session."""
    history = await short_term_memory.get_history(session_id)
    return {"session_id": session_id, "messages": history}


@app.get("/api/tools")
async def list_tools():
    """List registered tools. Authentication will be added in the next P0 batch."""
    return {"tools": mcp_server.list_tools()}


@app.post("/api/tools/call")
async def call_tool(request: ToolCallRequest):
    """Call a registered tool with a validated envelope."""
    result = await mcp_server.call_tool(name=request.name, arguments=request.arguments)
    return {
        "success": result.success,
        "result": result.result,
        "error": result.error,
        "duration_ms": result.duration_ms,
    }


@app.get("/api/metrics")
async def get_metrics():
    """Return in-process metrics. Authentication will be added in the next P0 batch."""
    return {
        "agent_metrics": metrics.get_summary(),
        "tool_call_log": mcp_server.get_call_log(last_n=20),
    }


@app.get("/health")
async def health_check():
    """Liveness probe: the HTTP process is accepting requests."""
    return {"status": "healthy", "version": "1.0.0"}


@app.get("/ready")
async def readiness_check():
    """Readiness probe for the graph and configured required dependencies."""
    short_term_status = await short_term_memory.health_status()
    report = build_readiness_report(
        graph_initialized=graph is not None,
        short_term=short_term_status,
        long_term=long_term_memory.health_status(),
        require_redis=settings.require_redis,
        require_rag_index=settings.require_rag_index,
    )
    return JSONResponse(status_code=200 if report["ready"] else 503, content=report)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "api.main:app",
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8000")),
        reload=os.getenv("RELOAD", "false").strip().lower() == "true",
    )
