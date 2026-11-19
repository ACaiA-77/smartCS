"""Authenticated read-only workspace endpoints for the Personal Copilot Web UI."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

from auth.context import UserContext
from auth.dependency import get_current_user
from internal_api.context import summarize_pending_target
from internal_api.write_authorization import COPILOT_EXTERNAL_ACTIONS, COPILOT_LOCAL_WRITE_TOOLS
from knowledge_engine.vault import ALLOWED_ZONES, VaultSecurityError

router = APIRouter(prefix="/api", tags=["copilot-ui"])

_MAX_LIST_ITEMS = 15
_MAX_NOTE_PREVIEW_CHARS = 12000
_TWO_PHASE_TOOLS = COPILOT_EXTERNAL_ACTIONS | {"copilot_action_prepare", "copilot_action_confirm"}

_deps_getter: Callable[[], Mapping[str, Any]] = dict
_server_getter: Callable[[], Any] = lambda: None


def configure_copilot_web(
    *,
    dependencies_getter: Callable[[], Mapping[str, Any]],
    mcp_server_getter: Callable[[], Any],
) -> None:
    """Bind runtime dependency and tool-server lookups from ``api.main``."""

    global _deps_getter, _server_getter
    _deps_getter = dependencies_getter
    _server_getter = mcp_server_getter


def _get_deps() -> Mapping[str, Any]:
    return _deps_getter() or {}


def _list_vault_notes(vault: Any) -> tuple[dict[str, int], list[dict[str, Any]]]:
    zone_counts = {zone: 0 for zone in ALLOWED_ZONES}
    if vault is None:
        return zone_counts, []
    paths = list(vault.list_notes())
    for path in paths:
        rel = path.relative_to(vault.root).as_posix()
        zone = rel.split("/", 1)[0] if "/" in rel else ""
        if zone in zone_counts:
            zone_counts[zone] += 1

    def _mtime(path: Any) -> float:
        try:
            return path.stat().st_mtime
        except OSError:
            return 0.0

    recent_paths = sorted(paths, key=_mtime, reverse=True)[:_MAX_LIST_ITEMS]
    notes: list[dict[str, Any]] = []
    for path in recent_paths:
        rel = path.relative_to(vault.root).as_posix()
        try:
            note = vault.read_note(rel)
            notes.append(
                {
                    "path": note["path"],
                    "title": note["title"],
                    "zone": note["zone"],
                    "tags": note["tags"],
                }
            )
        except (OSError, VaultSecurityError, FileNotFoundError):
            continue
    return zone_counts, notes


def _action_tier(name: str, op_type: str) -> str:
    if name in _TWO_PHASE_TOOLS:
        return "external_two_phase"
    if op_type == "write" or name in COPILOT_LOCAL_WRITE_TOOLS:
        return "local_write"
    return "read"


def _list_tools() -> tuple[dict[str, int], list[dict[str, Any]]]:
    server = _server_getter()
    if server is None:
        return {"total": 0, "read": 0, "write": 0, "local_write": 0, "external_two_phase": 0}, []
    raw_tools = server.list_tools()
    copilot_tools: list[dict[str, Any]] = []
    read_count = 0
    write_count = 0
    local_write_count = 0
    external_two_phase_count = 0
    for item in raw_tools:
        name = str(item.get("name") or "")
        category = str(item.get("category") or "")
        if category != "copilot" and name != "knowledge_search":
            continue
        op_type = str(item.get("operationType") or item.get("operation_type") or "read").lower()
        tier = _action_tier(name, op_type)
        if tier == "external_two_phase":
            write_count += 1
            external_two_phase_count += 1
        elif tier == "local_write":
            write_count += 1
            local_write_count += 1
        else:
            read_count += 1
        copilot_tools.append(
            {
                "name": name,
                "description": item.get("description") or "",
                "operation_type": op_type,
                "action_tier": tier,
                "risk_level": item.get("riskLevel") or item.get("risk_level") or "low",
                "requires_confirmation": bool(
                    item.get("requiresConfirmation")
                    if "requiresConfirmation" in item
                    else item.get("requires_confirmation", False)
                ),
            }
        )
    return {
        "total": len(copilot_tools),
        "read": read_count,
        "write": write_count,
        "local_write": local_write_count,
        "external_two_phase": external_two_phase_count,
    }, copilot_tools


def _format_expires_at(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    return str(value or "")


async def _load_user_pending_actions(request: Request, business_user_id: str) -> list[dict[str, Any]]:
    database = getattr(request.app.state, "platform_database", None)
    if database is None or not business_user_id:
        return []

    def read(_connection, cursor):
        cursor.execute(
            """SELECT id, session_id, type, payload, expires_at
               FROM pending_action
               WHERE user_id=%s AND type='tool' AND status='pending'
                 AND expires_at > CURRENT_TIMESTAMP(3)
               ORDER BY created_at DESC
               LIMIT %s""",
            (business_user_id, _MAX_LIST_ITEMS),
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
        summary = summarize_pending_target(tool, arguments)
        if not summary and arguments:
            summary = ", ".join(f"{k}={v}" for k, v in list(arguments.items())[:3])
        items.append(
            {
                "pending_action_id": str(row["id"]),
                "session_id": str(row.get("session_id") or ""),
                "tool": tool,
                "target_summary": summary,
                "arguments_preview": {k: str(v)[:160] for k, v in arguments.items()},
                "expires_at": _format_expires_at(row.get("expires_at")),
            }
        )
    return items


@router.get("/ui/profile")
async def ui_profile(response: Response) -> dict[str, str]:
    """Return the active UI profile (single-track Personal Copilot)."""

    response.headers["Cache-Control"] = "no-store"
    return {"profile": "copilot"}


@router.get("/copilot/context")
async def copilot_context(
    request: Request,
    response: Response,
    user: UserContext = Depends(get_current_user),
) -> dict[str, Any]:
    """Return read-only Copilot connector, vault, ontology, tool, and pending-action summaries."""

    deps = _get_deps()
    response.headers["Cache-Control"] = "no-store"

    vault = deps.get("vault")
    ontology = deps.get("ontology")
    mail_router = deps.get("mail_router")
    github_client = deps.get("github_client")
    feishu_client = deps.get("feishu_client")

    zone_counts, notes = _list_vault_notes(vault)

    entities: list[dict[str, Any]] = []
    entity_count = 0
    if ontology is not None:
        all_entities = ontology.query_entities(limit=500)
        entity_count = len(all_entities)
        entities = [
            {
                "id": item["id"],
                "entity_type": item["entity_type"],
                "name": item["name"],
                "canonical_key": item["canonical_key"],
            }
            for item in all_entities[:_MAX_LIST_ITEMS]
        ]

    tool_summary, tools = _list_tools()
    pending_actions = await _load_user_pending_actions(request, user.business_user_id)

    return {
        "profile": "copilot",
        "connectors": {
            "obsidian": {
                "enabled": vault is not None,
                "total_notes": sum(zone_counts.values()),
                "zones": zone_counts,
            },
            "ontology": {
                "enabled": ontology is not None,
                "entity_count": entity_count,
            },
            "mail": {
                "enabled": mail_router is not None,
                "accounts": list(mail_router.accounts) if mail_router is not None else [],
            },
            "github": {
                "enabled": github_client is not None,
            },
            "feishu": {
                "enabled": feishu_client is not None,
            },
        },
        "notes": notes,
        "entities": entities,
        "tool_summary": tool_summary,
        "tools": tools,
        "pending_actions": pending_actions,
    }


@router.get("/copilot/vault/note")
async def copilot_vault_note(
    response: Response,
    path: str = Query(..., min_length=1, max_length=512),
    _user: UserContext = Depends(get_current_user),
) -> dict[str, Any]:
    """Read a single sandboxed Obsidian note for preview in the side panel."""

    deps = _get_deps()
    response.headers["Cache-Control"] = "no-store"
    vault = deps.get("vault")
    if vault is None:
        raise HTTPException(status_code=404, detail="obsidian vault is not configured")
    try:
        note = vault.read_note(path)
    except VaultSecurityError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    content = str(note.get("content") or "")
    truncated = len(content) > _MAX_NOTE_PREVIEW_CHARS
    if truncated:
        content = content[:_MAX_NOTE_PREVIEW_CHARS] + "\n\n...（预览已截断）"
    return {
        "path": note["path"],
        "title": note["title"],
        "zone": note["zone"],
        "tags": note["tags"],
        "links": note["links"],
        "content": content,
        "truncated": truncated,
    }


@router.get("/copilot/ontology/entity/{entity_id}")
async def copilot_ontology_entity(
    entity_id: str,
    response: Response,
    _user: UserContext = Depends(get_current_user),
) -> dict[str, Any]:
    """Read one ontology entity and its direct graph neighbors."""

    deps = _get_deps()
    response.headers["Cache-Control"] = "no-store"
    ontology = deps.get("ontology")
    if ontology is None:
        raise HTTPException(status_code=404, detail="ontology store is not configured")
    entity = ontology.get_entity(entity_id)
    if entity is None:
        raise HTTPException(status_code=404, detail="ontology entity not found")
    neighbors = ontology.get_neighbors(entity_id)
    return {"entity": entity, "neighbors": neighbors}
