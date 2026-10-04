"""GET /internal/ready — cheap dependency readiness for the pi-harness.

Phase 11 §4.2. `/health` stays a pure liveness answer ("the process is up");
this is the readiness answer ("this runtime can serve a turn"), and the harness
probes it from `/ready`.

It must stay CHEAP and must do no business work — no LLM call, no RAG
retrieval, no order/refund/ticket operation. Three booleans:

    platform_db     one `SELECT 1` through PlatformDatabase
    tool_runtime    the tool executor is wired and its order store answers
    memory_runtime  the memory service is initialized and its store answers

The response carries no URLs, credentials, SQL or driver text: a failing check
is reported as `false`, and the reason stays in the runtime's own logs.

Authentication is the same internal service credential every other /internal
route uses. This endpoint belongs to no turn, so the token carries no
account/session claims (see `decode_ops_service_token`).
"""

from __future__ import annotations

import asyncio

import jwt as pyjwt
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from internal_api.service_jwt import ServiceAuthUnavailable, decode_ops_service_token
from internal_api.tools import _bearer_token

router = APIRouter(prefix="/internal", tags=["internal"])


async def _platform_ok(request: Request) -> bool:
    database = getattr(request.app.state, "platform_database", None)
    if database is None:
        return False
    try:
        await database.ping()
        return True
    except Exception:
        # Reachability only; the driver error is never surfaced to the caller.
        return False


async def _tool_runtime_ok(request: Request) -> bool:
    executor = getattr(request.app.state, "tool_executor", None)
    repository = getattr(request.app.state, "order_repository", None)
    if executor is None or repository is None:
        return False
    try:
        # Blocking SQLite call on a tiny demo store: off the event loop.
        await asyncio.to_thread(repository.ping)
        return True
    except Exception:
        return False


def _memory_runtime_ok(request: Request, platform_ok: bool) -> bool:
    # The memory service keeps its state in the platform database, so "wired"
    # plus "store reachable" is the honest answer; a second connection would
    # only re-probe the same server.
    return getattr(request.app.state, "user_memory_service", None) is not None and platform_ok


@router.get("/ready")
async def ready(request: Request) -> JSONResponse:
    token = _bearer_token(request)
    try:
        decode_ops_service_token(token)
    except ServiceAuthUnavailable:
        return JSONResponse(
            status_code=503,
            content={"ok": False, "platform_db": False, "tool_runtime": False, "memory_runtime": False},
        )
    except pyjwt.InvalidTokenError:
        return JSONResponse(status_code=401, content={"detail": "invalid service authentication"})

    platform_ok = await _platform_ok(request)
    tool_ok = await _tool_runtime_ok(request)
    memory_ok = _memory_runtime_ok(request, platform_ok)
    # `ok` is the conjunction: a runtime that cannot reach one of the three is
    # not a runtime the harness should route user traffic to. (The harness's own
    # `/ready` applies the softer rule to the MEMORY OUTBOX BACKLOG — a queue
    # that is draining is a degradation, a missing subsystem is not.)
    body = {
        "ok": platform_ok and tool_ok and memory_ok,
        "platform_db": platform_ok,
        "tool_runtime": tool_ok,
        "memory_runtime": memory_ok,
    }
    return JSONResponse(status_code=200 if body["ok"] else 503, content=body)
