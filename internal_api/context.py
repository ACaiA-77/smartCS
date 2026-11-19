"""POST /internal/context/turn-snapshot — the per-turn Copilot context snapshot.

Design rules:
  * Deterministic and LLM-free — assembled from authoritative stores only;
  * `protected_fields` reads active `pending_action` rows from MySQL rather than
    the transcript, so compaction never loses an active confirmation target;
  * Read-only: never invokes an LLM or mutates business state.
"""

from __future__ import annotations

from copy import deepcopy
import json
import os
from typing import Any

import jwt as pyjwt
from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

from auth.context import UserContext, current_user
from internal_api.auth import resolve_service_session
from internal_api.service_jwt import ServiceAuthUnavailable, decode_service_token
from internal_api.tools import _bearer_token, _error

router = APIRouter(prefix="/internal", tags=["internal"])

PROTECTED_KEYS = {
    "pending_action_id",
    "action_id",
    "user_id",
    "client_request_id",
    "idempotency_key",
    "timestamp",
    "pending_action",
    "clientRequestId",
    "idempotencyKey",
}

DEFAULT_TOKEN_BUDGET = 1200


def _set_path(target: dict[str, Any], path: list[str], value: Any) -> None:
    cursor = target
    for item in path[:-1]:
        nested = cursor.get(item)
        if not isinstance(nested, dict):
            nested = {}
            cursor[item] = nested
        cursor = nested
    cursor[path[-1]] = deepcopy(value)


def _merge_nested(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    for key, value in right.items():
        if isinstance(value, dict):
            if not isinstance(left.get(key), dict):
                left[key] = {}
            _merge_nested(left[key], value)
        else:
            left[key] = deepcopy(value)
    return left


def extract_protected_fields(value: Any, *, prefix: list[str] | None = None) -> dict[str, Any]:
    """Extract protected identifiers/actions without copying arbitrary tool args."""
    prefix = prefix or []
    found: dict[str, Any] = {}
    if isinstance(value, dict):
        for key, item in value.items():
            path = [*prefix, str(key)]
            if str(key) in PROTECTED_KEYS:
                _set_path(found, path, item)
            nested = extract_protected_fields(item, prefix=path)
            _merge_nested(found, nested)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            nested = extract_protected_fields(item, prefix=[*prefix, str(index)])
            _merge_nested(found, nested)
    return found


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


def _render_pending_actions(pending: list[dict[str, Any]], limit: int) -> str:
    lines: list[str] = []
    for item in pending[:limit]:
        action_id = item.get("pending_action_id") or ""
        tool = item.get("tool") or "tool"
        summary = item.get("target_summary") or ""
        suffix = f" ({summary})" if summary else ""
        lines.append(f"- 待确认操作 {action_id}: {tool}{suffix}")
    return "\n".join(lines)


def summarize_pending_target(tool: str, arguments: dict[str, Any]) -> str:
    """Summarize the business target of a pending Copilot external action."""
    fields = {
        "mail_send": ("account", "to", "subject"),
        "gh_create_issue": ("owner", "repo", "title"),
        "gh_post_comment": ("owner", "repo", "number"),
        "feishu_send_message": ("receive_id", "msg_type"),
    }.get(tool, ())
    parts = [f"{field}={arguments[field]}" for field in fields if field in arguments]
    return ", ".join(parts)


_summarize_pending_target = summarize_pending_target


async def _load_pending_actions(request: Request, session_id: str, business_user_id: str) -> list[dict[str, Any]]:
    database = getattr(request.app.state, "platform_database", None)
    if database is None:
        return []

    def read(_connection, cursor):
        cursor.execute(
            """SELECT id, type, payload, expires_at
               FROM pending_action
               WHERE session_id=%s AND user_id=%s AND status='pending'
                 AND expires_at > CURRENT_TIMESTAMP(3)
               ORDER BY created_at DESC
               LIMIT 3""",
            (session_id, business_user_id),
        )
        return cursor.fetchall() or []

    try:
        rows = await database._call(read)
    except Exception:
        return []

    items: list[dict[str, Any]] = []
    for row in rows:
        raw_payload = row.get("payload")
        if isinstance(raw_payload, str):
            try:
                payload = json.loads(raw_payload)
            except ValueError:
                payload = {}
        else:
            payload = dict(raw_payload or {})
        tool = str(payload.get("tool") or row.get("type") or "tool")
        arguments = payload.get("arguments") if isinstance(payload.get("arguments"), dict) else {}
        items.append(
            {
                "pending_action_id": str(row["id"]),
                "tool": tool,
                "target_summary": _summarize_pending_target(tool, arguments),
            }
        )
    return items


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

    user = UserContext(
        account_id=service.account_id,
        username=str(account.get("username") or ""),
        business_user_id=business_user_id,
    )
    context_token = current_user.set(user)
    try:
        blocks: list[dict[str, Any]] = []

        # (1) protected fields — active two-phase pending actions from MySQL.
        pending_actions = await _load_pending_actions(request, body.session_id, business_user_id)
        if pending_actions:
            protected = extract_protected_fields({"pending_actions": pending_actions})
            blocks.append(
                {
                    "kind": "protected_fields",
                    "title": "待确认外部操作（以此为准）",
                    "content": _render_pending_actions(pending_actions, 3),
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


__all__ = ["router", "SnapshotBody", "PROTECTED_KEYS", "extract_protected_fields"]
