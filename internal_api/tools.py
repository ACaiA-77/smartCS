"""POST /internal/tools/execute — the READ-only business tool channel.

Contract (phase2-design.md §2):

    headers  Authorization: Bearer <Internal Service JWT>
             aud=smartcs-business-runtime · account_id · business_user_id (REQUIRED, D5)
             · session_id · client_request_id · iat · exp
    body     { "tool": "order_query", "arguments": {...},
               "session_id": "...", "client_request_id": "..." }   ← no identity fields
    200      { "ok": true, "content": "<minimal text>", "details": {...},
               "executor": { "toolCallId", "durationMs", "retries" } }
    4xx/5xx  { "ok": false, "error": { "code", "message" } }

Security rules, in order:
  1. service JWT verified with `business_user_id` REQUIRED and cross-checked
     against the database;
  2. the token's session/account must match the request body;
  3. session ownership is re-checked through the platform database;
  4. identity-looking fields in `arguments` are STRIPPED and audited, never
     trusted — the runtime force-binds `user_id` from the verified claims;
  5. only the five READ tools are reachable; anything else is refused here, so
     a compromised or confused harness still cannot reach a write tool.

The existing ToolExecutor / MCPToolServer is called unchanged; identity binding
reuses their `current_user` ContextVar mechanism rather than re-implementing it.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

import jwt as pyjwt
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from auth.context import UserContext, current_user
from internal_api.service_jwt import ServiceAuthUnavailable, decode_service_token
from internal_api.write_authorization import (
    LIVE_WRITE_TOOLS,
    AuthorizationDenied,
    WriteAuthorizationService,
)
from mcp.tool_execution import ToolExecutionContext as ToolExecutionContextType

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/internal", tags=["internal"])

#: The model-facing READ surface (plan v2 §6.4). Write tools are Phase 5 and are
#: deliberately absent: they are not merely filtered, they are unreachable.
READ_TOOLS = frozenset(
    {"knowledge_search", "order_query", "ticket_query", "refund_evaluate", "risk_check"}
)

#: Fields that look like authority. Stripped before the executor ever sees them,
#: so neither the model nor a compromised harness can assert an identity or
#: pre-confirm a write. `confirmed`/`approval_id` are included ahead of Phase 5
#: so the guard is already in place when write tools arrive.
IDENTITY_FIELDS = (
    "user_id",
    "business_user_id",
    "account_id",
    "session_id",
    "client_request_id",
    "confirmed",
    "approval_id",
)

#: Fields force-bound from the verified claims when the tool declares them.
FORCED_IDENTITY_FIELDS = ("user_id",)

_TRUNCATION_SUFFIX = "…<truncated>"


def _max_content_chars() -> int:
    """Align with the existing context preview budget (context/compression.py)."""
    raw = os.getenv("SMARTCS_TOOL_RESULT_MAX_CHARS") or os.getenv("SMARTCS_CONTEXT_TOOL_PREVIEW_CHARS") or "1200"
    try:
        return max(200, int(raw))
    except ValueError:
        return 1200


def _truncate(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[: max(0, limit - len(_TRUNCATION_SUFFIX))] + _TRUNCATION_SUFFIX, True


def render_content(result: Any, limit: int) -> tuple[str, bool]:
    """Render a bounded, mechanical summary of a tool result.

    Deliberately does NOT invent business semantics: it prints top-level scalar
    fields as `key=value`, collapses containers, and bounds every value. The
    full structure always travels in `details.result`.
    """
    if result is None:
        # A failed call often carries no payload; returning "" lets the caller
        # fall back to the executor's error message rather than the text "None".
        return "", False

    parts: list[str] = []

    def walk(prefix: str, value: Any, depth: int) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                name = f"{prefix}.{key}" if prefix else str(key)
                walk(name, item, depth + 1)
        elif isinstance(value, (list, tuple)):
            parts.append(f"{prefix}=[{len(value)} items]" if prefix else f"[{len(value)} items]")
            for index, item in enumerate(value[:3]):
                walk(f"{prefix}[{index}]", item, depth + 1)
        elif isinstance(value, str):
            parts.append(f"{prefix}={value}" if prefix else value)
        else:
            parts.append(f"{prefix}={value!r}" if prefix else repr(value))

    walk("", result, 0)
    return _truncate("\n".join(parts), limit)


def _live_writes_enabled() -> bool:
    """Live writes are opt-in per deployment; anything else stays read-only."""
    return os.getenv("SMARTCS_WRITE_MODE", "off").strip().lower() == "live"


class ToolExecuteBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tool: str = Field(min_length=1, max_length=128)
    arguments: dict[str, Any] = Field(default_factory=dict)
    session_id: str = Field(min_length=1, max_length=128)
    client_request_id: str = Field(min_length=1, max_length=128)


def _error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message})


def _bearer_token(request: Request) -> str:
    header = request.headers.get("authorization")
    if not header:
        raise _error(401, "service_authentication_required", "service authentication required")
    parts = header.split()
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1]:
        raise _error(401, "invalid_service_authentication", "invalid service authentication")
    return parts[1]


def _tool_declares_user_id(tool: Any) -> bool:
    schema = getattr(tool, "input_schema", None) or {}
    properties = schema.get("properties") if isinstance(schema, dict) else None
    return isinstance(properties, dict) and "user_id" in properties


@router.post("/tools/execute")
async def execute_tool(request: Request, body: ToolExecuteBody) -> dict[str, Any]:
    started = time.perf_counter()

    # (1) Caller must be the harness, and must name the end user it acts for.
    token = _bearer_token(request)
    try:
        service = decode_service_token(token, require_business_user_id=True)
    except ServiceAuthUnavailable:
        raise _error(503, "internal_authentication_not_configured", "internal authentication not configured") from None
    except pyjwt.InvalidTokenError:
        raise _error(401, "invalid_service_authentication", "invalid service authentication") from None

    # (2) The token must be for the session it claims to be serving.
    if service.session_id != body.session_id or service.client_request_id != body.client_request_id:
        raise _error(401, "invalid_service_authentication", "invalid service authentication")

    # (5a) READ tools take the unchanged path. WRITE tools are reachable only
    # when this deployment explicitly enabled live writes, and even then only
    # through the authorization service below.
    if body.tool not in READ_TOOLS:
        if body.tool not in LIVE_WRITE_TOOLS or not _live_writes_enabled():
            raise _error(403, "tool_not_allowed_on_internal_channel", f"tool '{body.tool}' is not available here")
        return await _execute_live_write(request, service, body)

    executor = getattr(request.app.state, "tool_executor", None)
    if executor is None:
        raise _error(503, "tool_executor_unavailable", "tool executor unavailable")
    sessions = getattr(request.app.state, "platform_sessions", None)
    users = getattr(request.app.state, "platform_users", None)
    if sessions is None or users is None:
        raise _error(503, "platform_database_unavailable", "platform database unavailable")

    # (3) Ownership + identity cross-checks against the database.
    account = await users.by_id(service.account_id)
    if (
        not account
        or account.get("status") != "active"
        or not isinstance(account.get("business_user_id"), str)
        or account["business_user_id"] != service.business_user_id
    ):
        raise _error(403, "identity_mismatch", "service identity does not match an active account")
    if await sessions.get_owned(body.session_id, service.account_id) is None:
        raise _error(404, "session_not_found", "session not found")

    # (4) Strip anything that looks like authority before the executor sees it.
    arguments = dict(body.arguments)
    stripped = [field for field in IDENTITY_FIELDS if field in arguments]
    for field in stripped:
        arguments.pop(field, None)
    if stripped:
        # Audit trail: who tried to assert what, for which session.
        logger.warning(
            "internal tool call stripped identity fields: tool=%s session=%s account=%s fields=%s",
            body.tool,
            body.session_id,
            service.account_id,
            ",".join(sorted(stripped)),
        )

    tool = executor.server.get_tool(body.tool)
    if tool is None:
        raise _error(404, "unknown_tool", f"tool '{body.tool}' is not registered")

    # (4b) Force-bind identity from the verified claims. `customer_tool_arguments`
    # already rebinds for its customer set; doing it here as well covers tools
    # outside that set (notably risk_check) so no tool ever sees model-supplied
    # identity.
    forced: list[str] = []
    if _tool_declares_user_id(tool):
        arguments["user_id"] = service.business_user_id
        forced.append("user_id")

    # Reuse the existing ContextVar contract so ToolExecutor's own binding and
    # any downstream context injection see the authenticated user.
    user = UserContext(
        account_id=service.account_id,
        username=str(account.get("username") or ""),
        business_user_id=service.business_user_id,
    )
    context_token = current_user.set(user)
    try:
        result = await executor.execute(body.tool, arguments)
    except PermissionError as exc:
        raise _error(403, "identity_mismatch", str(exc)) from None
    except (TypeError, ValueError) as exc:
        # Unknown/mistyped arguments: the tool contract rejected the call.
        raise _error(400, "invalid_arguments", str(exc)) from None
    finally:
        current_user.reset(context_token)

    # Phase 5b: in live mode a successful evaluation opens the two-phase flow by
    # producing a REAL pending_action in MySQL (the authority for the second
    # phase). shadow/off keep the Phase 4 behaviour of a deterministic
    # placeholder id derived in TS — this branch is the only difference.
    pending_action_id: str | None = None
    if body.tool == "refund_evaluate" and _live_writes_enabled() and result.success:
        platform = getattr(request.app.state, "platform_database", None)
        evaluation = result.result if isinstance(result.result, dict) else {}
        payload = {
            "order_id": arguments.get("order_id"),
            "user_id": service.business_user_id,
            "amount": evaluation.get("refund_amount") or evaluation.get("amount"),
            "refund_mode": evaluation.get("refund_mode"),
            "reason": "用户确认后退款",
        }
        if payload["order_id"] and platform is not None:
            try:
                created = await WriteAuthorizationService(platform).create_pending_action(
                    session_id=body.session_id,
                    user_id=service.business_user_id,
                    action_type="refund_create",
                    payload=payload,
                )
                pending_action_id = created["pending_action_id"]
            except Exception:
                # A missing pending action degrades the flow to a refusal on the
                # confirm turn; it must never turn the READ into an error.
                logger.warning("could not create pending_action for %s", body.session_id, exc_info=True)

    duration_ms = (time.perf_counter() - started) * 1000
    content, content_truncated = render_content(result.result, _max_content_chars())
    if pending_action_id:
        content = f"{content}\npending_action_id={pending_action_id}"
    if not result.success and not content.strip():
        content = result.error or result.error_code or "tool call failed"

    details: dict[str, Any] = {
        "tool": body.tool,
        # Exact executor result: parity with a direct ToolExecutor call is
        # asserted against this field.
        "result": result.result,
        "status": result.status,
        "success": result.success,
        "errorCode": result.error_code,
        "error": result.error,
        "attempts": result.attempts,
        "operationType": result.operation_type,
        "riskLevel": result.risk_level,
        "requiresConfirmation": result.requires_confirmation,
        "contentTruncated": content_truncated,
        "audit": {"strippedFields": sorted(stripped), "forcedFields": sorted(forced)},
    }
    if pending_action_id:
        details["pending_action_id"] = pending_action_id

    return {
        "ok": True,
        "content": content,
        "details": json.loads(json.dumps(details, default=str)),
        "executor": {
            "toolCallId": f"{body.session_id}:{body.client_request_id}:{body.tool}",
            "durationMs": round(duration_ms, 3),
            "retries": max(0, int(result.attempts) - 1),
        },
    }


async def _execute_live_write(request: Request, service, body: ToolExecuteBody) -> dict[str, Any]:
    """Authorize, then execute, a real write. **The order is load-bearing.**

    1. the authorization service reads the durable user message and decides;
    2. the operation id becomes durable in receipt (+ pending_action);
    3. ONLY THEN is the executor called, with `confirmed=True` computed here.

    `confirmed` never comes from the caller: the body schema forbids extra
    fields, identity-looking fields are stripped, and the flag is constructed by
    this function from the authorization result.
    """
    executor = getattr(request.app.state, "tool_executor", None)
    platform = getattr(request.app.state, "platform_database", None)
    if executor is None or platform is None:
        raise _error(503, "tool_executor_unavailable", "tool executor unavailable")

    authorization = WriteAuthorizationService(platform)

    arguments = dict(body.arguments)
    stripped = [field for field in IDENTITY_FIELDS if field in arguments]
    for field in stripped:
        arguments.pop(field, None)
    if stripped:
        logger.warning(
            "internal live write stripped identity fields: tool=%s session=%s fields=%s",
            body.tool,
            body.session_id,
            ",".join(sorted(stripped)),
        )

    tool = executor.server.get_tool(body.tool)
    # refund_confirm has no registration of its own: it resolves to the
    # low-level refund_create that stays inside Python.
    if tool is None and body.tool == "refund_confirm":
        tool = executor.server.get_tool("refund_create")
    if tool is None:
        raise _error(404, "unknown_tool", f"tool '{body.tool}' is not registered")

    # --- server-authoritative fields, injected BEFORE authorization --------
    # Stripping was right: a model-supplied idempotency key or payload hash is
    # trust-sensitive (it could be used to collide with, or evade, another
    # request's idempotency record). But ticket_create's contract *requires*
    # them, so the service supplies its own — the same pattern as force-binding
    # `user_id`. Everything downstream therefore sees a payload whose identity
    # and idempotency fields are ours, and the authorized target_hash covers
    # exactly what will be sent.
    execute_as = body.tool
    if _tool_declares_user_id(tool):
        arguments["user_id"] = service.business_user_id
    if body.tool == "ticket_create":
        from tickets.service import canonical_ticket_payload_hash  # read-only import

        arguments["client_request_id"] = body.client_request_id
        arguments["request_payload_hash"] = canonical_ticket_payload_hash(
            title=arguments.get("title"),
            description=arguments.get("description"),
            priority=arguments.get("priority"),
            category=arguments.get("category"),
            user_id=arguments.get("user_id"),
        )

    try:
        if body.tool == "ticket_create":
            decision = await authorization.authorize_ticket_create(
                session_id=body.session_id,
                client_request_id=body.client_request_id,
                arguments=arguments,
            )
        elif body.tool == "refund_confirm":
            decision = await authorization.authorize_refund_confirm(
                session_id=body.session_id,
                client_request_id=body.client_request_id,
                account_user_id=service.business_user_id,
                arguments=arguments,
            )
            # The model supplies only the pending id; the business parameters
            # come from the frozen pending snapshot, never from the model.
            #
            # The snapshot is an audit record and may carry evaluation-only
            # fields (amount, refund_mode) that the low-level tool does not
            # accept, so narrow it to the tool's declared schema — the schema is
            # the contract, the snapshot is the source of the values.
            snapshot = dict(decision.arguments or {})
            declared = (getattr(tool, "input_schema", None) or {}).get("properties") or {}
            arguments = {key: value for key, value in snapshot.items() if key in declared}
            arguments["user_id"] = service.business_user_id
            # The low-level tool is refund_create — it is never exposed to the
            # model (plan v2 §6.4); this is the only path that reaches it.
            execute_as = "refund_create"
        else:
            raise AuthorizationDenied(
                "not_enabled", f"{body.tool} is not enabled on this deployment"
            )
    except AuthorizationDenied as exc:
        # A business refusal is a RESULT the model must see, not an HTTP failure.
        return {
            "ok": True,
            "content": exc.message,
            "details": {
                "tool": body.tool,
                "authorized": False,
                "executed": False,
                "errorCode": exc.code,
                "audit": {"strippedFields": sorted(stripped)},
            },
            "executor": {
                "toolCallId": f"{body.session_id}:{body.client_request_id}:{body.tool}",
                "durationMs": 0.0,
                "retries": 0,
            },
        }

    user = UserContext(
        account_id=service.account_id,
        username="",
        business_user_id=service.business_user_id,
    )
    context_token = current_user.set(user)
    try:
        result = await executor.execute(
            execute_as,
            arguments,
            # Authorization computed by THIS service; never supplied by the caller.
            ToolExecutionContextType(confirmed=True, idempotency_key=decision.operation_id),
        )
    except BaseException:
        # The write may or may not have landed — that is exactly UNKNOWN.
        await authorization.mark_operation_state(
            body.session_id, body.client_request_id, decision.operation_id, "UNKNOWN"
        )
        raise
    finally:
        current_user.reset(context_token)

    # `executed` must reflect the BUSINESS outcome, not merely "the handler did
    # not raise": several handlers report failure by returning a dict with
    # `success=false` (deliberately, so callers get a structured reason).
    business_ok = result.success and not (
        isinstance(result.result, dict) and result.result.get("success") is False
    )
    if not business_ok and isinstance(result.result, dict):
        reason = result.result.get("reason_code")
        if isinstance(reason, str) and not result.error_code:
            result.error_code = reason

    await authorization.mark_operation_state(
        body.session_id,
        body.client_request_id,
        decision.operation_id,
        "COMPLETED" if business_ok else "FAILED",
    )

    # Only a successful refund retires the pending action. A failed one leaves
    # it pending so the user can retry with the same confirmation phrase (the
    # legacy handler behaves the same way).
    if business_ok and decision.pending_action_id:
        await authorization.consume_pending_action(decision.pending_action_id, decision.operation_id)

    content, content_truncated = render_content(result.result, _max_content_chars())
    if not business_ok and not content.strip():
        content = result.error or result.error_code or "write failed"

    return {
        "ok": True,
        "content": content,
        "details": {
            "tool": body.tool,
            "authorized": True,
            "executed": business_ok,
            "operationId": decision.operation_id,
            "result": result.result,
            "status": result.status,
            "success": result.success,
            "errorCode": result.error_code,
            "error": result.error,
            "attempts": result.attempts,
            "operationType": result.operation_type,
            "riskLevel": result.risk_level,
            "requiresConfirmation": result.requires_confirmation,
            "contentTruncated": content_truncated,
            "audit": {"strippedFields": sorted(stripped), "forcedFields": ["user_id"]},
        },
        "executor": {
            "toolCallId": f"{body.session_id}:{body.client_request_id}:{body.tool}",
            "durationMs": 0.0,
            "retries": max(0, int(result.attempts) - 1),
        },
    }
