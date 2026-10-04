"""WriteAuthorizationService — the only place a WRITE may be authorized.

Plan v2 §6.4: `confirmed=True` is **never** a model parameter. The model supplies
business intent and arguments; this service computes the authorization from
database authority plus the verified service identity, then hands the executor a
`ToolExecutionContext(confirmed=True, idempotency_key=<operation_id>)`.

Three rules, in order:
  1. the raw user message is read back from `memory_source_event` (the durable
     ledger written before the model ran) — never from the request body, so a
     compromised or confused harness cannot smuggle consent text;
  2. the deterministic phrase rules decide, and they are **copied** from the
     legacy handlers (see the provenance note below) so legacy behaviour is not
     disturbed;
  3. the operation id is made durable in `pending_action.operation_id` AND
     `agent_run_receipt.open_write_operations` **before** the write is sent
     (plan v2 §6.3 iron rule — otherwise F4/F5 are unrecoverable).

PROVENANCE / SYNC OBLIGATION
    The consent phrase tables below are copied verbatim from
    `agents/ticket_handler.py::_has_explicit_create_consent` (Phase 5a) and
    `agents/refund_handler.py` (Phase 5b). `agents/` is READ-ONLY this phase and
    legacy still uses its own copy, so the two now exist in parallel by design.
    When legacy is retired, one of them must be deleted — until then, any change
    to the legacy rule MUST be mirrored here.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

PENDING_TTL_MINUTES = 30

#: Tool operation types this service is allowed to authorize.
LIVE_WRITE_TOOLS = frozenset({"ticket_create", "refund_confirm"})


class AuthorizationDenied(Exception):
    """Deterministic business refusal. The model gets a result, not an error."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def has_explicit_create_consent(user_message: str) -> bool:
    """Copied from agents/ticket_handler.py::_has_explicit_create_consent.

    LLM intent alone cannot authorize a ticket-creating side effect.
    """
    normalized = re.sub(r"\s+", "", str(user_message))
    if any(
        marker in normalized
        for marker in (
            "怎么", "如何", "怎样", "流程", "步骤", "规则", "政策",
            "条件", "入口", "在哪里", "需要什么", "需要哪些", "怎么办",
            "是什么", "有哪些要求",
        )
    ):
        return False
    if "工单" in normalized and any(
        phrase in normalized for phrase in ("帮我创建", "我要创建", "请创建", "提交工单", "发起工单")
    ):
        return True
    return any(
        phrase in normalized
        for phrase in (
            "我要投诉", "帮我投诉", "我要申请理赔", "帮我申请理赔", "我要开户",
            "帮我办理开户", "提交投诉", "提交申请", "创建工单", "发起申请",
        )
    )


@dataclass(frozen=True)
class AuthorizationDecision:
    operation_id: str
    tool: str
    target_hash: str
    pending_action_id: str | None
    #: Authoritative arguments the executor must use (refund_confirm: the
    #: pending action's frozen snapshot, never the model's parameters).
    arguments: dict[str, Any] | None = None


def _canonical_hash(arguments: dict[str, Any]) -> str:
    import hashlib

    canonical = json.dumps(arguments, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class WriteAuthorizationService:
    """Authoritative WRITE gate. Stateless apart from the database handle."""

    def __init__(self, database):
        self.db = database

    # -- provenance ---------------------------------------------------------

    async def raw_user_message(self, session_id: str, client_request_id: str) -> str | None:
        """Read the durable USER_MESSAGE text. Fails closed when absent."""

        def read(_connection, cursor):
            cursor.execute(
                """SELECT content FROM memory_source_event
                   WHERE session_id=%s AND client_request_id=%s AND cleared_at IS NULL
                   LIMIT 1""",
                (session_id, client_request_id),
            )
            row = cursor.fetchone()
            return row["content"] if row else None

        return await self.db._call(read)

    # -- durable operation id (before send) ---------------------------------

    async def reserve_operation(
        self,
        *,
        session_id: str,
        client_request_id: str,
        tool: str,
        target_hash: str,
        pending_action_id: str | None = None,
    ) -> str:
        """Durably record the operation id BEFORE any write is sent.

        Writes to both authorities the design names: the receipt's
        `open_write_operations` (so a crashed request is recoverable) and, for
        the two-phase flow, `pending_action.operation_id`.
        """
        operation_id = uuid.uuid4().hex
        entry = {
            "operation_id": operation_id,
            "tool": tool,
            "target_hash": target_hash,
            "state": "PREPARED",
        }

        def write(_connection, cursor):
            cursor.execute(
                """SELECT open_write_operations FROM agent_run_receipt
                   WHERE session_id=%s AND client_request_id=%s LIMIT 1""",
                (session_id, client_request_id),
            )
            row = cursor.fetchone()
            existing = row["open_write_operations"] if row else None
            if isinstance(existing, str):
                try:
                    existing = json.loads(existing)
                except json.JSONDecodeError:
                    existing = []
            operations = list(existing or [])
            operations.append(entry)
            cursor.execute(
                """UPDATE agent_run_receipt SET open_write_operations=CAST(%s AS JSON)
                   WHERE session_id=%s AND client_request_id=%s""",
                (json.dumps(operations), session_id, client_request_id),
            )
            if cursor.rowcount != 1:
                # No receipt means there is no durable home for the operation
                # id; refusing is the only safe answer.
                raise AuthorizationDenied(
                    "receipt_missing", "no durable receipt for this request; refusing to write"
                )
            if pending_action_id is not None:
                cursor.execute(
                    """UPDATE pending_action SET operation_id=%s
                       WHERE id=%s AND status='pending' AND operation_id IS NULL""",
                    (operation_id, pending_action_id),
                )
                if cursor.rowcount != 1:
                    raise AuthorizationDenied(
                        "pending_action_not_reserved",
                        "pending action could not be reserved for this operation",
                    )
            return True

        await self.db._call(write)
        return operation_id

    async def mark_operation_state(
        self, session_id: str, client_request_id: str, operation_id: str, state: str
    ) -> None:
        """Record the observed outcome (COMPLETED / FAILED / UNKNOWN) on the receipt."""

        def write(_connection, cursor):
            cursor.execute(
                """SELECT open_write_operations FROM agent_run_receipt
                   WHERE session_id=%s AND client_request_id=%s LIMIT 1""",
                (session_id, client_request_id),
            )
            row = cursor.fetchone()
            if not row:
                return False
            operations = row["open_write_operations"]
            if isinstance(operations, str):
                try:
                    operations = json.loads(operations)
                except json.JSONDecodeError:
                    operations = []
            updated = [
                {**item, "state": state} if item.get("operation_id") == operation_id else item
                for item in (operations or [])
            ]
            cursor.execute(
                """UPDATE agent_run_receipt SET open_write_operations=CAST(%s AS JSON)
                   WHERE session_id=%s AND client_request_id=%s""",
                (json.dumps(updated), session_id, client_request_id),
            )
            return True

        await self.db._call(write)

    # -- ticket_create (EXPLICIT_SAME_TURN) ---------------------------------

    async def authorize_ticket_create(
        self, *, session_id: str, client_request_id: str, arguments: dict[str, Any]
    ) -> AuthorizationDecision:
        """Same-turn explicit consent, read from the durable user message."""
        message = await self.raw_user_message(session_id, client_request_id)
        if message is None:
            raise AuthorizationDenied(
                "provenance_unavailable", "cannot verify user consent for this request"
            )
        if not has_explicit_create_consent(message):
            raise AuthorizationDenied(
                "explicit_consent_required",
                "用户未在本轮明确要求创建工单，请先确认后再创建。",
            )
        operation_id = await self.reserve_operation(
            session_id=session_id,
            client_request_id=client_request_id,
            tool="ticket_create",
            target_hash=_canonical_hash(arguments),
        )
        return AuthorizationDecision(
            operation_id=operation_id,
            tool="ticket_create",
            target_hash=_canonical_hash(arguments),
            pending_action_id=None,
        )

    # -- pending_action (refund two-phase, Phase 5b) ------------------------

    async def create_pending_action(
        self, *, session_id: str, user_id: str, action_type: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        pending_id = str(uuid.uuid4())
        expires_at = datetime.now(timezone.utc) + timedelta(minutes=PENDING_TTL_MINUTES)

        def write(_connection, cursor):
            cursor.execute(
                """INSERT INTO pending_action (id, session_id, user_id, type, payload, status, expires_at)
                   VALUES (%s,%s,%s,%s,CAST(%s AS JSON),'pending',%s)""",
                (pending_id, session_id, user_id, action_type, json.dumps(payload), expires_at),
            )
            return True

        await self.db._call(write)
        return {"pending_action_id": pending_id, "expires_at": expires_at.isoformat()}

    async def load_pending_action(self, pending_id: str) -> dict[str, Any] | None:
        def read(_connection, cursor):
            cursor.execute("SELECT * FROM pending_action WHERE id=%s LIMIT 1", (pending_id,))
            return cursor.fetchone()

        return await self.db._call(read)

    async def expire_pending_action(self, pending_id: str) -> bool:
        """Atomic transition pending -> expired. Only one caller can win."""

        def write(_connection, cursor):
            cursor.execute(
                """UPDATE pending_action SET status='expired'
                   WHERE id=%s AND status='pending' AND expires_at <= CURRENT_TIMESTAMP(3)""",
                (pending_id,),
            )
            return cursor.rowcount == 1

        return await self.db._call(write)

    async def consume_pending_action(self, pending_id: str, operation_id: str) -> bool:
        def write(_connection, cursor):
            cursor.execute(
                """UPDATE pending_action SET status='consumed'
                   WHERE id=%s AND status='pending' AND operation_id=%s""",
                (pending_id, operation_id),
            )
            return cursor.rowcount == 1

        return await self.db._call(write)


# --- refund two-phase (Phase 5b) -------------------------------------------

_ORDER_ID_RE = re.compile(r"ORD[_-]?\d{8}[_-]?\d{4}", re.IGNORECASE)


def is_refund_confirmation(user_message: str) -> bool:
    """Deterministic confirmation rule.

    Mirrors the legacy intent router's mapping (`agents/refund_handler.py`):
    the confirmation turn is the one that says 确认退款 (and the cancel phrases
    are explicitly NOT confirmations). Kept deliberately narrow — a model's
    paraphrase is not consent.
    """
    normalized = re.sub(r"\s+", "", str(user_message))
    if any(phrase in normalized for phrase in ("取消退款", "不退了", "先不退", "算了")):
        return False
    return "确认退款" in normalized or "确认提交退款" in normalized


class _RefundAuthorization:
    """refund_confirm authorization, attached to WriteAuthorizationService below.

    A plain function assignment rather than inheritance gymnastics: the service
    is one class with one database handle, and re-basing it at import time would
    be a needless MRO hazard.
    """

    async def authorize_refund_confirm(
        self, *, session_id: str, client_request_id: str, account_user_id: str, arguments: dict[str, Any]
    ) -> AuthorizationDecision:
        pending_id = str(arguments.get("pending_action_id") or "").strip()
        if not pending_id:
            raise AuthorizationDenied("missing_pending_action", "缺少待确认的退款申请")

        pending = await self.load_pending_action(pending_id)
        if pending is None:
            raise AuthorizationDenied("pending_action_not_found", "退款申请不存在或已失效")
        if str(pending["session_id"]) != session_id:
            raise AuthorizationDenied("pending_action_session_mismatch", "退款申请不属于当前会话")
        if str(pending["user_id"]) != account_user_id:
            # Someone else's pending action: fail closed, never execute.
            raise AuthorizationDenied("pending_action_owner_mismatch", "退款申请不属于当前用户")

        if str(pending["status"]) != "pending":
            raise AuthorizationDenied("pending_action_not_pending", "退款申请已处理或已取消")

        # Expiry is an atomic transition, so two concurrent confirms cannot both
        # find it live.
        if await self.expire_pending_action(pending_id):
            raise AuthorizationDenied("pending_action_expired", "退款申请已过期，请重新发起评估")

        message = await self.raw_user_message(session_id, client_request_id)
        if message is None:
            raise AuthorizationDenied("provenance_unavailable", "无法核验本轮用户确认")
        if not is_refund_confirmation(message):
            raise AuthorizationDenied(
                "explicit_confirmation_required", "未检测到明确的退款确认，请回复“确认退款”。"
            )

        payload = pending["payload"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        payload = dict(payload or {})

        # If the confirmation turn names an order, it must be the pending one
        # (same rule as the legacy handler).
        named = _ORDER_ID_RE.search(message)
        if named and named.group(0).replace("_", "-").upper() != str(payload.get("order_id", "")).upper():
            raise AuthorizationDenied("order_mismatch", "本轮订单号与待确认订单不匹配，未提交退款")

        operation_id = await self.reserve_operation(
            session_id=session_id,
            client_request_id=client_request_id,
            tool="refund_confirm",
            target_hash=_canonical_hash(payload),
            pending_action_id=pending_id,
        )
        return AuthorizationDecision(
            operation_id=operation_id,
            tool="refund_confirm",
            target_hash=_canonical_hash(payload),
            pending_action_id=pending_id,
            arguments=payload,
        )


# Attach the refund flow to the service (see _RefundAuthorization docstring).
WriteAuthorizationService.authorize_refund_confirm = _RefundAuthorization.authorize_refund_confirm
