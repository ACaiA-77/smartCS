"""GET /internal/ready — cheap dependency readiness for the pi-harness.

`/health` stays a pure liveness answer ("the process is up"); this is the
readiness answer ("this runtime can serve a turn"), and the harness probes it
from `/ready`.

Three booleans:

    platform_db     one `SELECT 1` through PlatformDatabase
    tool_runtime    the tool executor is wired and its SQLite ledger answers
    memory_runtime  the memory service is initialized and its store answers
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
        return False


async def _tool_runtime_ok(request: Request) -> bool:
    executor = getattr(request.app.state, "tool_executor", None)
    ledger = getattr(request.app.state, "execution_ledger", None)
    if executor is None or ledger is None:
        return False
    try:
        await asyncio.to_thread(ledger.ping)
        return True
    except Exception:
        return False


def _memory_runtime_ok(request: Request, platform_ok: bool) -> bool:
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
    body = {
        "ok": platform_ok and tool_ok and memory_ok,
        "platform_db": platform_ok,
        "tool_runtime": tool_ok,
        "memory_runtime": memory_ok,
    }
    return JSONResponse(status_code=200 if body["ok"] else 503, content=body)
