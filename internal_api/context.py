"""POST /internal/context/turn-snapshot — the per-turn business context snapshot.

Contract (phase3-design.md §2):

    body  { "session_id": "...", "client_request_id": "..." }
    resp  { "blocks": [ { "kind", "title", "content", "priority" } ],
            "tokenBudget": <char ceiling> }

Design rules honoured here:
  * Deterministic and LLM-free — the snapshot is assembled from authoritative
    stores only, so the same request always yields the same blocks;
  * `protected_fields` comes from Python authority (the order repository), never
    from the transcript, which is what makes F10 hold: once compaction eats the
    history, the facts are still re-injected every turn;
  * the existing `context.compression` extractor defines which fields count as
    protected, so this endpoint does not invent its own list.

Read-only: it never calls `invoke_agent`, never writes anything.
"""

from __future__ import annotations

import os
from typing import Any

import jwt as pyjwt
from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

from auth.context import UserContext, current_user
from context.compression import extract_protected_fields
from internal_api.auth import resolve_service_session
from internal_api.service_jwt import ServiceAuthUnavailable, decode_service_token
from internal_api.tools import _bearer_token, _error

router = APIRouter(prefix="/internal", tags=["internal"])

#: Total character ceiling for the injected snapshot. Bounded so the snapshot
#: can never crowd out the conversation itself.
DEFAULT_TOKEN_BUDGET = 1200


def _token_budget() -> int:
    raw = os.getenv("SMARTCS_TURN_SNAPSHOT_BUDGET", str(DEFAULT_TOKEN_BUDGET))
    try:
        return max(200, int(raw))
    except ValueError:
        return DEFAULT_TOKEN_BUDGET


class SnapshotBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str = Field(min_length=1, max_length=128)
    client_request_id: str = Field(min_length=1, max_length=128)


def _bounded(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 16)] + "…<truncated>"


def _render_cards(cards: list[dict[str, Any]], limit: int) -> str:
    lines: list[str] = []
    for card in cards[:limit]:
        category = card.get("category") or card.get("kind") or "profile"
        key = card.get("key") or card.get("name") or ""
        value = card.get("value") or card.get("content") or ""
        if key or value:
            lines.append(f"- [{category}] {key}: {value}")
    return "\n".join(lines)


def _render_episodes(episodes: list[dict[str, Any]], limit: int) -> str:
    lines: list[str] = []
    for episode in episodes[:limit]:
        summary = episode.get("summary") or episode.get("content") or ""
        if summary:
            lines.append(f"- {summary}")
    return "\n".join(lines)


def _render_orders(orders: list[dict[str, Any]], limit: int) -> str:
    lines: list[str] = []
    for order in orders[:limit]:
        lines.append(
            f"- 订单 {order.get('order_id')}: {order.get('status_label') or order.get('status')}"
            f"，金额 {order.get('pay_amount')}，商品 {order.get('product') or ''}".rstrip("，")
        )
    return "\n".join(lines)


@router.post("/context/turn-snapshot")
async def turn_snapshot(request: Request, body: SnapshotBody) -> dict[str, Any]:
    token = _bearer_token(request)
    try:
        service = decode_service_token(token, require_business_user_id=True)
    except ServiceAuthUnavailable:
        raise _error(503, "internal_authentication_not_configured", "internal authentication not configured") from None
    except pyjwt.InvalidTokenError:
        raise _error(401, "invalid_service_authentication", "invalid service authentication") from None

    account, session = await resolve_service_session(request, service, body.session_id)
    business_user_id = service.business_user_id

    # A ContextVar so any downstream helper that reads `current_user` (e.g. the
    # order repository's owner-scoped reads) sees the verified identity.
    user = UserContext(
        account_id=service.account_id,
        username=str(account.get("username") or ""),
        business_user_id=business_user_id,
    )
    context_token = current_user.set(user)
    try:
        blocks: list[dict[str, Any]] = []

        # (1) protected fields — Python-authoritative business facts. Highest
        # priority: these are the values the model must not re-derive.
        order_repository = getattr(request.app.state, "order_repository", None)
        orders: list[dict[str, Any]] = []
        if order_repository is not None:
            try:
                orders = order_repository.list_orders_for_user(business_user_id, limit=3) or []
            except Exception:  # a snapshot must never fail the turn
                orders = []
        if orders:
            protected = extract_protected_fields({"orders": orders})
            blocks.append(
                {
                    "kind": "protected_fields",
                    "title": "权威业务事实（以此为准）",
                    "content": _render_orders(orders, 3),
                    "priority": 5,
                    "protected": protected,
                }
            )

        # (2) long-term memory highlights — read path only.
        user_memory = getattr(request.app.state, "user_memory_service", None)
        if user_memory is not None:
            repository = getattr(user_memory, "repository", None)
            cards: list[dict[str, Any]] = []
            episodes: list[dict[str, Any]] = []
            if repository is not None:
                try:
                    cards = await repository.profile_cards(business_user_id, limit=3) or []
                except Exception:
                    cards = []
                try:
                    episodes = await repository.episodes(business_user_id, limit=3) or []
                except Exception:
                    episodes = []
            content = "\n".join(part for part in (_render_cards(cards, 3), _render_episodes(episodes, 3)) if part)
            if content:
                blocks.append(
                    {
                        "kind": "memory_highlights",
                        "title": "用户长期记忆摘要",
                        "content": content,
                        "priority": 20,
                    }
                )

        # (3) user profile — verified identity, never model-supplied.
        blocks.append(
            {
                "kind": "user_profile",
                "title": "当前用户",
                "content": f"business_user_id={business_user_id}；会话 {body.session_id}",
                "priority": 10,
            }
        )

        budget = _token_budget()
        used = 0
        bounded_blocks: list[dict[str, Any]] = []
        for block in sorted(blocks, key=lambda item: item["priority"]):
            remaining = budget - used
            if remaining <= 40:
                break
            content = _bounded(str(block["content"]), remaining)
            block = {**block, "content": content}
            used += len(content)
            bounded_blocks.append(block)

        return {
            "blocks": bounded_blocks,
            "tokenBudget": budget,
            "session_id": body.session_id,
            "harness_version": session.get("harness_version") or "legacy",
        }
    finally:
        current_user.reset(context_token)


__all__ = ["router", "SnapshotBody"]
