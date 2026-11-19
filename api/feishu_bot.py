"""Feishu event callback and interactive-card approval endpoints.

Two routes make the Copilot reachable from Feishu:

* ``POST /api/integrations/feishu/events`` — URL verification and
  ``im.message.receive_v1`` text messages. A reply that carries a
  ``pending_action_id`` becomes an interactive card with 确认执行 / 取消 buttons;
  the card is sent through the configured :class:`FeishuClient`.
* ``POST /api/integrations/feishu/card-action`` — the button callback. Confirm
  is translated back into the confirmation phrase the agent's consent rules
  understand, and the turn is replayed into the same session; cancel directly
  updates ``pending_action`` to ``cancelled`` in the store without invoking the
  model.

The router is transport only. Whether a write is allowed never depends on this
module: the agent's ``copilot_action_confirm`` still goes through the runtime's
frozen-snapshot authorization.
"""

from __future__ import annotations

import hmac
import inspect
import json
import logging
import os
import uuid
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from connectors.feishu import FeishuClient, verify_feishu_signature
from internal_api.write_authorization import WriteAuthorizationService
from platform_db.database import PlatformUnavailable

router = APIRouter(prefix="/api/integrations/feishu", tags=["feishu"])
logger = logging.getLogger(__name__)

ChatRunner = Callable[[str, str, str], Awaitable[Any]]

_chat_runner: ChatRunner | None = None
_feishu_client: FeishuClient | None = None
_write_authorization: WriteAuthorizationService | None = None
_open_id_bindings: dict[str, str] | None = None

CONFIRM_PHRASE = "确认执行"
CANCEL_PHRASE = "取消"
DECISION_PHRASES = {"confirm": CONFIRM_PHRASE, "cancel": CANCEL_PHRASE}


def configure_chat_runner(runner: ChatRunner | None) -> None:
    """Install the agent entry point used for Feishu turns (tests, deployments).

    The runner receives ``(session_id, account_id, text)`` and may return a
    string or a mapping with ``response``/``reply``/``text`` and an optional
    ``pending_action_id``.
    """

    global _chat_runner
    _chat_runner = runner


def configure_feishu_client(client: FeishuClient | None) -> None:
    """Install the client used to send confirmation cards."""

    global _feishu_client
    _feishu_client = client


def configure_write_authorization(store: WriteAuthorizationService | None) -> None:
    """Install the write-authorization store used by card cancel actions (tests)."""

    global _write_authorization
    _write_authorization = store


def configure_open_id_bindings(bindings: Mapping[str, str] | None) -> None:
    """Install trusted Feishu open_id -> business_user_id bindings (tests)."""

    global _open_id_bindings
    if bindings is None:
        _open_id_bindings = None
        return
    _open_id_bindings = {
        str(open_id).strip(): str(business_user_id).strip()
        for open_id, business_user_id in bindings.items()
        if str(open_id).strip() and str(business_user_id).strip()
    }


def _resolve_write_authorization(request: Request) -> WriteAuthorizationService:
    if _write_authorization is not None:
        return _write_authorization
    store = getattr(request.app.state, "write_authorization", None)
    if store is not None:
        return store
    database = getattr(request.app.state, "platform_database", None)
    if database is None:
        raise HTTPException(status_code=503, detail="platform database unavailable")
    return WriteAuthorizationService(database)


def _env_text(name: str) -> str:
    return (os.getenv(name) or "").strip()


def _require_authenticated_callback() -> None:
    if not (_env_text("FEISHU_ENCRYPT_KEY") or _env_text("FEISHU_VERIFICATION_TOKEN")):
        raise HTTPException(
            status_code=503,
            detail="feishu callback authentication is not configured",
        )


def _resolve_feishu_business_user_id(open_id: str) -> str:
    if _open_id_bindings is not None:
        bindings = _open_id_bindings
    else:
        owner_open_id = _env_text("FEISHU_OWNER_OPEN_ID")
        business_user_id = _env_text("FEISHU_CHAT_BUSINESS_USER_ID")
        bindings = {owner_open_id: business_user_id} if owner_open_id and business_user_id else {}

    if not bindings:
        raise HTTPException(status_code=503, detail="feishu owner binding is not configured")
    if not open_id:
        raise HTTPException(status_code=403, detail="feishu operator open_id is required")
    for candidate_open_id, mapped_user_id in bindings.items():
        if hmac.compare_digest(open_id, candidate_open_id):
            return mapped_user_id
    raise HTTPException(status_code=403, detail="feishu operator is not authorized")


async def _default_chat_runner(session_id: str, account_id: str, text: str) -> dict[str, Any]:
    """Forward a Feishu turn to the pi harness using service-level settings.

    Feishu messages carry no end-user JWT, so the deployment supplies the
    account it acts as (``FEISHU_CHAT_*``). When those are absent the runner
    reports a configuration error rather than guessing an identity.
    """

    raw_account = _env_text("FEISHU_CHAT_ACCOUNT_ID")
    business_user_id = _env_text("FEISHU_CHAT_BUSINESS_USER_ID")
    user_token = _env_text("FEISHU_CHAT_USER_TOKEN")
    if not raw_account:
        raise RuntimeError(
            "Feishu chat runner is not configured: set FEISHU_CHAT_ACCOUNT_ID, "
            "FEISHU_CHAT_BUSINESS_USER_ID and FEISHU_CHAT_USER_TOKEN"
        )
    try:
        account_number = int(raw_account)
    except ValueError:
        raise RuntimeError("FEISHU_CHAT_ACCOUNT_ID must be an integer") from None
    if not business_user_id or not user_token:
        raise RuntimeError(
            "Feishu chat runner is not configured: set FEISHU_CHAT_ACCOUNT_ID, "
            "FEISHU_CHAT_BUSINESS_USER_ID and FEISHU_CHAT_USER_TOKEN"
        )

    from internal_api.harness_client import HarnessUnavailable, forward_chat

    try:
        response = await forward_chat(
            account_id=account_number,
            business_user_id=business_user_id,
            session_id=session_id,
            client_request_id=str(uuid.uuid4()),
            message=text,
            user_token=user_token,
        )
    except HarnessUnavailable as exc:
        raise RuntimeError(f"pi harness unavailable: {exc}") from exc
    if response.status_code != 200:
        raise RuntimeError(f"pi harness rejected the request: {response.status_code}")
    payload = response.json()
    message = payload.get("message") if isinstance(payload, dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str) or not content:
        raise RuntimeError("pi harness returned an invalid response")
    return {"response": content, "session_id": session_id, "account_id": account_id}


async def _read_json(request: Request) -> tuple[dict[str, Any], bytes]:
    raw = await request.body()
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HTTPException(status_code=400, detail="invalid feishu payload") from None
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="invalid feishu payload")
    return payload, raw


def _verify_request(request: Request, payload: dict[str, Any], raw: bytes) -> None:
    """Verify the signature and, when configured, the event token."""

    encrypt_key = _env_text("FEISHU_ENCRYPT_KEY")
    if encrypt_key:
        timestamp = request.headers.get("x-lark-request-timestamp", "")
        nonce = request.headers.get("x-lark-request-nonce", "")
        signature = request.headers.get("x-lark-signature", "")
        if not verify_feishu_signature(timestamp, nonce, encrypt_key, raw, signature):
            raise HTTPException(status_code=401, detail="invalid feishu signature")

    verification_token = _env_text("FEISHU_VERIFICATION_TOKEN")
    if verification_token:
        header = payload.get("header")
        supplied = payload.get("token")
        if supplied is None and isinstance(header, dict):
            supplied = header.get("token")
        if not isinstance(supplied, str) or not hmac.compare_digest(
            supplied, verification_token
        ):
            raise HTTPException(status_code=401, detail="invalid feishu verification token")


def _message_text(message: dict[str, Any]) -> str | None:
    msg_type = str(message.get("message_type") or message.get("msg_type") or "text").lower()
    if msg_type != "text":
        return None
    content = message.get("content")
    if isinstance(content, dict):
        nested = content
    elif isinstance(content, str):
        try:
            nested = json.loads(content)
        except json.JSONDecodeError:
            return content
    else:
        nested = None
    if isinstance(nested, dict):
        text = nested.get("text")
        if isinstance(text, str):
            return text
    text = message.get("text")
    return text if isinstance(text, str) else None


def _event_parts(payload: dict[str, Any]) -> tuple[str | None, str | None, str | None]:
    """Return (event_type, open_id, chat_id) across Feishu v1/v2 envelopes."""

    event = payload.get("event")
    event = event if isinstance(event, dict) else {}
    header = payload.get("header")
    header = header if isinstance(header, dict) else {}
    event_type = header.get("event_type") or event.get("type") or payload.get("type")

    sender = event.get("sender")
    sender = sender if isinstance(sender, dict) else {}
    sender_id = sender.get("sender_id")
    sender_id = sender_id if isinstance(sender_id, dict) else {}
    open_id = sender_id.get("open_id") or event.get("open_id")

    message = event.get("message")
    message = message if isinstance(message, dict) else {}
    chat_id = message.get("chat_id")

    return (
        event_type if isinstance(event_type, str) else None,
        open_id if isinstance(open_id, str) else None,
        chat_id if isinstance(chat_id, str) else None,
    )


def _normalize_reply(result: Any) -> tuple[str, str | None]:
    if isinstance(result, str):
        return result, None
    if isinstance(result, dict):
        for key in ("reply", "response", "content", "text"):
            value = result.get(key)
            if isinstance(value, str) and value:
                pending = result.get("pending_action_id")
                return value, pending if isinstance(pending, str) and pending else None
        pending = result.get("pending_action_id")
        return "", pending if isinstance(pending, str) and pending else None
    if result is None:
        return "", None
    return str(result), None


def _button(text: str, decision: str, session_id: str, pending_action_id: str, kind: str) -> dict[str, Any]:
    return {
        "tag": "button",
        "text": {"tag": "plain_text", "content": text},
        "type": kind,
        "value": {
            "decision": decision,
            "session_id": session_id,
            "pending_action_id": pending_action_id,
        },
    }


def build_confirmation_card(
    text: str, *, session_id: str, pending_action_id: str
) -> dict[str, Any]:
    """Build the interactive confirmation card for an external action."""

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "orange",
            "title": {"tag": "plain_text", "content": "需要你确认的操作"},
        },
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": text or "请确认是否执行该操作。"}},
            {
                "tag": "action",
                "actions": [
                    _button(CONFIRM_PHRASE, "confirm", session_id, pending_action_id, "primary"),
                    _button(CANCEL_PHRASE, "cancel", session_id, pending_action_id, "danger"),
                ],
            },
        ],
    }


def _result_card(title: str, text: str, template: str) -> dict[str, Any]:
    return {
        "config": {"wide_screen_mode": True},
        "header": {"template": template, "title": {"tag": "plain_text", "content": title}},
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": text or title}},
        ],
    }


async def _send_card(chat_id: str, card: dict[str, Any]) -> bool:
    client = _feishu_client
    if client is None:
        return False
    try:
        result = client.send_message(chat_id, "chat_id", "interactive", card)
        if inspect.isawaitable(result):
            await result
    except Exception as exc:  # best-effort delivery; the JSON response still reports the card
        logger.warning("failed to send Feishu card: %s", exc)
        return False
    return True


async def _run_turn(session_id: str, account_id: str, text: str) -> tuple[str, str | None]:
    runner = _chat_runner or _default_chat_runner
    result = await runner(session_id, account_id, text)
    return _normalize_reply(result)


@router.post("/events")
async def feishu_events(request: Request) -> dict[str, Any]:
    """Feishu event callback: URL verification and incoming text messages."""

    payload, raw = await _read_json(request)
    _verify_request(request, payload, raw)

    if payload.get("type") == "url_verification":
        challenge = payload.get("challenge")
        if not isinstance(challenge, str):
            raise HTTPException(status_code=400, detail="missing feishu challenge")
        return {"challenge": challenge}

    event_type, open_id, chat_id = _event_parts(payload)
    if event_type != "im.message.receive_v1":
        return {"status": "ignored", "event_type": event_type}

    event = payload.get("event")
    message = event.get("message") if isinstance(event, dict) else None
    message = message if isinstance(message, dict) else {}
    text = _message_text(message)

    if not open_id or not chat_id:
        raise HTTPException(status_code=400, detail="feishu message is missing open_id or chat_id")
    if text is None or not text.strip():
        return {"status": "ignored", "event_type": event_type, "reason": "unsupported message"}

    reply, pending_action_id = await _run_turn(chat_id, open_id, text)

    card = None
    sent = False
    if pending_action_id:
        card = build_confirmation_card(
            reply, session_id=chat_id, pending_action_id=pending_action_id
        )
        sent = await _send_card(chat_id, card)

    return {
        "status": "ok",
        "event_type": event_type,
        "session_id": chat_id,
        "account_id": open_id,
        "reply": reply,
        "pending_action_id": pending_action_id,
        "card": card,
        "sent": sent,
    }


def _card_action_parts(
    payload: dict[str, Any],
) -> tuple[str, str, str, str, str]:
    """Extract (decision, session_id, pending_action_id, open_id, context_chat_id).

    Supports both the real Feishu ``card.action.trigger`` v2.0 envelope and
    the flat card-action format used by existing tests.
    """

    header = payload.get("header") if isinstance(payload.get("header"), dict) else {}
    event_type = header.get("event_type")
    if isinstance(event_type, str) and event_type and event_type != "card.action.trigger":
        raise HTTPException(status_code=400, detail="unexpected feishu card callback event_type")

    event = payload.get("event") if isinstance(payload.get("event"), dict) else {}
    action = event.get("action") if isinstance(event.get("action"), dict) else payload.get("action")
    raw_value = action.get("value") if isinstance(action, dict) else None
    if isinstance(raw_value, str):
        try:
            raw_value = json.loads(raw_value)
        except json.JSONDecodeError:
            raw_value = None
    value = raw_value if isinstance(raw_value, dict) else {}

    decision = str(value.get("decision") or "").strip().lower()
    if decision not in DECISION_PHRASES:
        raise HTTPException(status_code=400, detail="unknown feishu card decision")

    session_id = value.get("session_id")
    pending_action_id = value.get("pending_action_id")
    if not isinstance(session_id, str) or not session_id:
        raise HTTPException(status_code=400, detail="missing session_id")
    if not isinstance(pending_action_id, str) or not pending_action_id:
        raise HTTPException(status_code=400, detail="missing pending_action_id")

    operator = event.get("operator") if isinstance(event.get("operator"), dict) else payload.get("operator")
    operator = operator if isinstance(operator, dict) else {}
    operator_open_id = operator.get("open_id") if isinstance(operator.get("open_id"), str) else ""
    top_open_id = payload.get("open_id") if isinstance(payload.get("open_id"), str) else ""
    if operator_open_id and top_open_id and operator_open_id != top_open_id:
        raise HTTPException(status_code=400, detail="conflicting feishu operator open_id")
    open_id = (operator_open_id or top_open_id).strip()

    context = (
        event.get("context")
        if isinstance(event.get("context"), dict)
        else (payload.get("context") if isinstance(payload.get("context"), dict) else {})
    )
    raw_chat_id = context.get("open_chat_id")
    if not isinstance(raw_chat_id, str):
        raw_chat_id = payload.get("open_chat_id")
    context_chat_id = (raw_chat_id if isinstance(raw_chat_id, str) else "").strip()
    return decision, session_id, pending_action_id, open_id, context_chat_id


def _card_action_response(
    *,
    decision: str,
    phrase: str,
    session_id: str,
    pending_action_id: str,
    reply: str,
    title: str,
    template: str,
) -> dict[str, Any]:
    return {
        "status": "ok",
        "decision": decision,
        "phrase": phrase,
        "session_id": session_id,
        "pending_action_id": pending_action_id,
        "reply": reply,
        "card": _result_card(title, reply, template),
    }


@router.post("/card-action")
async def feishu_card_action(request: Request) -> dict[str, Any]:
    """Interactive-card button callback: confirm or cancel a pending action."""

    payload, raw = await _read_json(request)
    _verify_request(request, payload, raw)

    decision, session_id, pending_action_id, open_id, context_chat_id = _card_action_parts(payload)
    if context_chat_id and context_chat_id != session_id:
        raise HTTPException(status_code=409, detail="feishu card session mismatch")

    if decision == "cancel":
        _require_authenticated_callback()
        actor_business_user_id = _resolve_feishu_business_user_id(open_id)
        store = _resolve_write_authorization(request)
        try:
            pending = await store.load_pending_action(pending_action_id)
        except PlatformUnavailable:
            raise HTTPException(status_code=503, detail="platform database unavailable") from None
        if pending is None:
            raise HTTPException(status_code=404, detail="pending action not found")
        if str(pending.get("user_id") or "") != actor_business_user_id:
            raise HTTPException(status_code=403, detail="pending action does not belong to feishu operator")
        if str(pending.get("session_id") or "") != session_id:
            raise HTTPException(status_code=409, detail="pending action session mismatch")

        try:
            cancelled = await store.cancel_pending_action(
                pending_action_id,
                session_id=session_id,
                user_id=actor_business_user_id,
            )
        except PlatformUnavailable:
            raise HTTPException(status_code=503, detail="platform database unavailable") from None
        if not cancelled:
            raise HTTPException(status_code=409, detail="pending action is no longer pending")

        return _card_action_response(
            decision=decision,
            phrase=CANCEL_PHRASE,
            session_id=session_id,
            pending_action_id=pending_action_id,
            reply="已取消该待确认操作。",
            title="已取消",
            template="grey",
        )

    phrase = DECISION_PHRASES[decision]
    reply, _ = await _run_turn(session_id, open_id, phrase)
    return _card_action_response(
        decision=decision,
        phrase=phrase,
        session_id=session_id,
        pending_action_id=pending_action_id,
        reply=reply,
        title="已确认执行",
        template="green",
    )


__all__ = [
    "CANCEL_PHRASE",
    "CONFIRM_PHRASE",
    "build_confirmation_card",
    "configure_chat_runner",
    "configure_feishu_client",
    "configure_open_id_bindings",
    "configure_write_authorization",
    "router",
]
