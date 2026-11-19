"""Authenticated workspace endpoints for the Personal Copilot Web UI."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

from api.copilot_bootstrap import obsidian_rag_document_count
from auth.context import UserContext
from auth.dependency import get_current_user
from internal_api.context import parse_pending_payload, summarize_pending_target
from internal_api.write_authorization import (
    COPILOT_EXTERNAL_ACTIONS,
    COPILOT_LOCAL_WRITE_TOOLS,
    WriteAuthorizationService,
)
from knowledge_engine.vault import READ_ZONES, VaultSecurityError
from platform_db.database import PlatformUnavailable
from tool_runtime.copilot_tools import search_obsidian_vault

router = APIRouter(prefix="/api", tags=["copilot-ui"])

_MAX_LIST_ITEMS = 15
_MAX_NOTE_PREVIEW_CHARS = 12000
_TWO_PHASE_TOOLS = COPILOT_EXTERNAL_ACTIONS | {"copilot_action_prepare", "copilot_action_confirm"}

_deps_getter: Callable[[], Mapping[str, Any]] = dict
_server_getter: Callable[[], Any] = lambda: None
_obsidian_retriever_getter: Callable[[], Any] = lambda: None


def configure_copilot_web(
    *,
    dependencies_getter: Callable[[], Mapping[str, Any]],
    mcp_server_getter: Callable[[], Any],
    obsidian_retriever_getter: Callable[[], Any] | None = None,
) -> None:
    """Bind runtime dependency and tool-server lookups from ``api.main``."""

    global _deps_getter, _server_getter, _obsidian_retriever_getter
    _deps_getter = dependencies_getter
    _server_getter = mcp_server_getter
    if obsidian_retriever_getter is not None:
        _obsidian_retriever_getter = obsidian_retriever_getter


def _get_deps() -> Mapping[str, Any]:
    return _deps_getter() or {}


def _get_obsidian_retriever() -> Any:
    return _obsidian_retriever_getter()


def _note_mtime(path: Any) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _note_summary(note: dict[str, Any]) -> dict[str, Any]:
    return {
        "path": note["path"],
        "title": note["title"],
        "zone": note["zone"],
        "tags": note["tags"],
    }


def _read_note_summary_items(
    vault: Any,
    paths: list[Any],
    *,
    limit: int,
    needle: str = "",
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for path in sorted(paths, key=_note_mtime, reverse=True):
        rel = vault.relative_path(path)
        try:
            note = vault.read_note(rel)
        except (OSError, UnicodeDecodeError, VaultSecurityError, FileNotFoundError):
            continue
        if needle:
            haystack = " ".join(
                [note["title"], note["path"], note["zone"], " ".join(note["tags"])]
            ).casefold()
            if needle not in haystack:
                continue
        items.append(_note_summary(note))
        if len(items) >= limit:
            break
    return items


def _list_vault_notes(vault: Any, *, limit: int = _MAX_LIST_ITEMS) -> tuple[dict[str, int], list[dict[str, Any]]]:
    zone_counts = {zone: 0 for zone in READ_ZONES}
    if vault is None:
        return zone_counts, []
    paths = list(vault.list_notes())
    for path in paths:
        zone = vault._zone_of(path)
        if zone in zone_counts:
            zone_counts[zone] += 1

    return zone_counts, _read_note_summary_items(vault, paths, limit=limit)


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

    try:
        rows = await WriteAuthorizationService(database).list_active_pending_actions(
            user_id=business_user_id, limit=_MAX_LIST_ITEMS
        )
    except Exception:
        return []

    items: list[dict[str, Any]] = []
    for row in rows:
        tool, arguments = parse_pending_payload(row)
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
    obsidian_retriever = _get_obsidian_retriever()
    obsidian_rag_docs = obsidian_rag_document_count(obsidian_retriever)

    return {
        "profile": "copilot",
        "connectors": {
            "obsidian": {
                "enabled": vault is not None,
                "source_enabled": bool(
                    vault is not None and (getattr(vault, "source_root", None) is not None or getattr(vault, "read_only", False))
                ),
                "rag_index_enabled": obsidian_retriever is not None,
                "rag_documents": obsidian_rag_docs,
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


@router.get("/copilot/vault/notes")
async def copilot_vault_notes(
    response: Response,
    zone: str | None = Query(default=None),
    q: str = Query(default="", max_length=256),
    limit: int = Query(default=50, ge=1, le=200),
    _user: UserContext = Depends(get_current_user),
) -> dict[str, Any]:
    """List sandboxed Obsidian notes across managed and read-only source zones."""

    deps = _get_deps()
    response.headers["Cache-Control"] = "no-store"
    vault = deps.get("vault")
    if vault is None:
        raise HTTPException(status_code=404, detail="obsidian vault is not configured")
    clean_zone = zone.strip() if zone is not None and zone.strip() else None
    try:
        paths = list(vault.list_notes(clean_zone))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    items = _read_note_summary_items(vault, paths, limit=limit, needle=q.strip().casefold())
    return {"total": len(paths), "notes": items}


@router.get("/copilot/vault/search")
async def copilot_vault_search(
    response: Response,
    query: str = Query(..., min_length=1, max_length=512),
    zone: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    mode: str = Query(default="hybrid"),
    _user: UserContext = Depends(get_current_user),
) -> dict[str, Any]:
    """Search Obsidian notes lexically and via the isolated Obsidian RAG index."""

    deps = _get_deps()
    response.headers["Cache-Control"] = "no-store"
    vault = deps.get("vault")
    if vault is None:
        raise HTTPException(status_code=404, detail="obsidian vault is not configured")
    clean_zone = zone.strip() if zone is not None and zone.strip() else None
    obsidian_retriever = _get_obsidian_retriever()
    clean_mode = mode.strip().lower()
    try:
        hits = await asyncio.to_thread(
            search_obsidian_vault,
            vault,
            query,
            zone=clean_zone,
            limit=limit,
            obsidian_retriever=obsidian_retriever,
            mode=clean_mode,
        )
    except (ValueError, VaultSecurityError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "query": query,
        "mode": clean_mode,
        "rag_enabled": obsidian_retriever is not None,
        "hits": hits,
    }


@router.get("/copilot/vault/backlinks")
async def copilot_vault_backlinks(
    response: Response,
    target: str = Query(..., min_length=1, max_length=512),
    _user: UserContext = Depends(get_current_user),
) -> dict[str, Any]:
    """Return sandboxed notes linking to ``target``."""

    deps = _get_deps()
    response.headers["Cache-Control"] = "no-store"
    vault = deps.get("vault")
    if vault is None:
        raise HTTPException(status_code=404, detail="obsidian vault is not configured")
    try:
        backlinks = vault.backlinks(target)
    except (ValueError, VaultSecurityError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"target": target, "backlinks": backlinks}


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
        backlinks = vault.backlinks(note["path"])
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
        "backlinks": backlinks,
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


@router.post("/copilot/pending/{pending_action_id}/cancel")
async def copilot_cancel_pending_action(
    pending_action_id: str,
    request: Request,
    response: Response,
    user: UserContext = Depends(get_current_user),
) -> dict[str, Any]:
    """Cancel an active pending action owned by the authenticated user."""

    response.headers["Cache-Control"] = "no-store"
    database = getattr(request.app.state, "platform_database", None)
    if database is None:
        raise HTTPException(status_code=503, detail="platform database unavailable")

    store = WriteAuthorizationService(database)
    try:
        pending = await store.load_pending_action(pending_action_id)
    except PlatformUnavailable:
        raise HTTPException(status_code=503, detail="platform database unavailable") from None

    if pending is None or str(pending.get("user_id") or "") != user.business_user_id:
        raise HTTPException(status_code=404, detail="pending action not found")

    session_id = str(pending.get("session_id") or "")
    try:
        cancelled = await store.cancel_pending_action(
            pending_action_id,
            session_id=session_id,
            user_id=user.business_user_id,
        )
    except PlatformUnavailable:
        raise HTTPException(status_code=503, detail="platform database unavailable") from None
    if not cancelled:
        raise HTTPException(status_code=409, detail="pending action is no longer pending")

    return {
        "pending_action_id": pending_action_id,
        "session_id": session_id,
        "status": "cancelled",
        "cancelled": True,
    }
