"""Copilot tool surface: Obsidian, ontology, mail, GitHub and Feishu.

The registrations here are *capabilities*, not authority.  Every WRITE tool is
marked ``requires_confirmation=True`` and ``retryable=False``; the internal API
(``internal_api/write_authorization.py``) is the only place that may set
``confirmed=True``, and external side effects additionally require the
``copilot_action_prepare`` -> ``copilot_action_confirm`` frozen-snapshot flow.

Only tools whose dependency is supplied are registered.  That keeps the
existing e-commerce tool surface byte-for-byte unchanged for callers that do
not pass any copilot dependency, while making each copilot tool provably
backed by a real object rather than a half-configured one.
"""

from __future__ import annotations

import asyncio
from typing import Any

from knowledge_engine.vault import READ_ZONES, VaultSecurityError
from tool_runtime.mcp_server import MCPToolServer

_READ_DEFAULTS: dict[str, Any] = {
    "category": "copilot",
    "operation_type": "read",
    "risk_level": "low",
    "requires_confirmation": False,
    "retryable": True,
}

_LOCAL_WRITE_DEFAULTS: dict[str, Any] = {
    "category": "copilot",
    "operation_type": "write",
    "risk_level": "low",
    "requires_confirmation": True,
    "retryable": False,
}

_EXTERNAL_WRITE_DEFAULTS: dict[str, Any] = {
    "category": "copilot",
    "operation_type": "write",
    "risk_level": "high",
    "requires_confirmation": True,
    "retryable": False,
}


def _path_text(path: Any) -> Any:
    """Render vault paths as strings so tool results are JSON serializable."""

    try:
        from pathlib import Path

        return Path(path).as_posix() if isinstance(path, Path) else path
    except (TypeError, ValueError):
        return path


def _compile_result(result: dict[str, Any]) -> dict[str, Any]:
    converted = dict(result)
    for field in ("raw_path", "wiki_path", "index_path", "log_path"):
        if field in converted:
            converted[field] = _path_text(converted[field])
    return converted


def search_obsidian_vault(
    vault,
    query: str,
    *,
    zone: str | None = None,
    limit: int = 10,
    obsidian_retriever=None,
    mode: str = "hybrid",
) -> list[dict[str, Any]]:
    """Search Obsidian notes lexically and (when configured) via the isolated Obsidian RAG index."""

    if vault is None:
        return []
    if zone is not None and zone not in READ_ZONES:
        raise ValueError(f"unknown vault zone: {zone}")
    if mode not in {"hybrid", "lexical", "rag"}:
        raise ValueError(f"unknown search mode: {mode}")
    bounded_limit = max(0, int(limit))
    clean_query = str(query or "").strip()
    if not clean_query or bounded_limit <= 0:
        return []

    lexical_hits = (
        vault.search(clean_query, zone=zone, limit=bounded_limit)
        if mode in {"hybrid", "lexical"}
        else []
    )
    if obsidian_retriever is None or mode == "lexical":
        return lexical_hits

    merged: dict[str, dict[str, Any]] = {}
    for item in lexical_hits:
        path_key = str(item["path"])
        merged[path_key] = {
            **item,
            "match_type": "lexical",
            "_lex": float(item.get("score") or 0.0),
            "_rag": None,
        }

    rag_top_k = (
        min(max(bounded_limit * 2, 12), 30)
        if zone is not None
        else min(max(bounded_limit, 10), 25)
    )
    rag_hits = obsidian_retriever.retrieve(
        clean_query,
        top_k=rag_top_k,
        rerank=True,
    )
    for raw_hit in rag_hits:
        meta = dict(getattr(raw_hit, "metadata", {}) or {})
        rel_candidate = str(meta.get("vault_path") or "").strip()
        if not rel_candidate:
            continue
        try:
            resolved = vault._resolve(rel_candidate)
        except (VaultSecurityError, ValueError, OSError):
            continue
        if not resolved.is_file():
            continue
        canonical_path = vault.relative_path(resolved)
        if canonical_path.startswith("wiki/proposals/"):
            continue
        hit_zone = vault._zone_of(resolved)
        if zone is not None and hit_zone != zone:
            continue
        rag_score = float(getattr(raw_hit, "score", 0.0) or 0.0)
        if canonical_path in merged:
            entry = merged[canonical_path]
            entry["match_type"] = "hybrid"
            if entry["_rag"] is None or rag_score > entry["_rag"]:
                entry["_rag"] = rag_score
                entry["rag_score"] = round(rag_score, 4)
            continue
        try:
            note = vault.read_note(canonical_path)
        except (VaultSecurityError, FileNotFoundError, OSError, UnicodeDecodeError):
            continue
        snippet_raw = " ".join(str(getattr(raw_hit, "content", "") or "").split())
        snippet = snippet_raw[:200] + ("…" if len(snippet_raw) > 200 else "")
        merged[canonical_path] = {
            "path": note["path"],
            "title": note["title"],
            "zone": note["zone"],
            "tags": note["tags"],
            "score": round(rag_score, 4),
            "rag_score": round(rag_score, 4),
            "snippet": snippet,
            "match_type": "rag",
            "_lex": 0.0,
            "_rag": rag_score,
        }

    ordered = sorted(
        merged.values(),
        key=lambda item: (
            -float(item["_lex"]),
            -(float(item["_rag"]) if item["_rag"] is not None else float("-inf")),
            str(item["path"]).casefold(),
        ),
    )[:bounded_limit]
    for item in ordered:
        item.pop("_lex", None)
        item.pop("_rag", None)
    return ordered


def register_copilot_tools(
    server: MCPToolServer,
    *,
    vault=None,
    ontology=None,
    wiki_compiler=None,
    mail_router=None,
    github_client=None,
    feishu_client=None,
    obsidian_retriever=None,
) -> MCPToolServer:
    """Register every copilot tool whose backing dependency is available."""

    # ------------------------------------------------------------- vault (read)
    if vault is not None:

        @server.register(
            name="vault_search",
            description="Search Obsidian notes by title, tag, content or personal RAG index",
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "zone": {"type": "string", "enum": ["wiki", "raw", "notes", "daily", "source"]},
                    "limit": {"type": "integer", "default": 10},
                },
                "required": ["query"],
            },
            **_READ_DEFAULTS,
        )
        async def vault_search(
            query: str, zone: str | None = None, limit: int = 10
        ) -> list[dict[str, Any]]:
            return await asyncio.to_thread(
                search_obsidian_vault,
                vault,
                query,
                zone=zone,
                limit=int(limit),
                obsidian_retriever=obsidian_retriever,
            )

        @server.register(
            name="vault_read_note",
            description="Read one Obsidian note and its links/tags",
            input_schema={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
            **_READ_DEFAULTS,
        )
        async def vault_read_note(path: str) -> dict[str, Any]:
            return vault.read_note(path)

        @server.register(
            name="vault_backlinks",
            description="List notes that link to a wikilink target",
            input_schema={
                "type": "object",
                "properties": {"target": {"type": "string"}},
                "required": ["target"],
            },
            **_READ_DEFAULTS,
        )
        async def vault_backlinks(target: str) -> list[dict[str, Any]]:
            return vault.backlinks(target)

        # ------------------------------------------------------- vault (write)
        @server.register(
            name="vault_append_daily",
            description="Append an entry to daily/YYYY-MM-DD.md",
            input_schema={
                "type": "object",
                "properties": {
                    "entry": {"type": "string"},
                    "date_str": {"type": "string", "description": "YYYY-MM-DD"},
                },
                "required": ["entry"],
            },
            **_LOCAL_WRITE_DEFAULTS,
        )
        async def vault_append_daily(entry: str, date_str: str | None = None) -> dict[str, Any]:
            path = vault.append_daily(entry, date_str=date_str)
            return {"path": _path_text(path), "appended": True}

        @server.register(
            name="vault_propose_edit",
            description="Propose (never apply) an edit to a note under notes/",
            input_schema={
                "type": "object",
                "properties": {
                    "target_rel_path": {"type": "string"},
                    "rationale": {"type": "string"},
                    "proposed_content": {"type": "string"},
                },
                "required": ["target_rel_path", "rationale", "proposed_content"],
            },
            **_LOCAL_WRITE_DEFAULTS,
        )
        async def vault_propose_edit(
            target_rel_path: str, rationale: str, proposed_content: str
        ) -> dict[str, Any]:
            path = vault.propose_note_edit(target_rel_path, rationale, proposed_content)
            return {"path": _path_text(path), "status": "proposed"}

    # ------------------------------------------------------------- wiki (read)
    if wiki_compiler is not None:

        @server.register(
            name="wiki_lint",
            description="Report broken links, orphan pages and unindexed wiki pages",
            input_schema={"type": "object", "properties": {}},
            **_READ_DEFAULTS,
        )
        async def wiki_lint() -> dict[str, Any]:
            return wiki_compiler.lint_wiki()

        @server.register(
            name="wiki_compile_doc",
            description="Archive a source and compile/update its wiki page",
            input_schema={
                "type": "object",
                "properties": {
                    "source_uri": {"type": "string"},
                    "title": {"type": "string"},
                    "raw_content": {"type": "string"},
                    "summary_markdown": {"type": "string"},
                    "slug": {"type": "string"},
                    "related_entities": {"type": "array", "items": {"type": "object"}},
                },
                "required": ["source_uri", "title", "raw_content", "summary_markdown"],
            },
            **_LOCAL_WRITE_DEFAULTS,
        )
        async def wiki_compile_doc(
            source_uri: str,
            title: str,
            raw_content: str,
            summary_markdown: str,
            slug: str | None = None,
            related_entities: list[dict] | None = None,
        ) -> dict[str, Any]:
            result = wiki_compiler.ingest_document(
                source_uri=source_uri,
                title=title,
                raw_content=raw_content,
                summary_markdown=summary_markdown,
                slug=slug,
                related_entities=related_entities,
            )
            return _compile_result(result)

    # --------------------------------------------------------- ontology (read)
    if ontology is not None:

        @server.register(
            name="onto_query",
            description="Query ontology entities by type and free text",
            input_schema={
                "type": "object",
                "properties": {
                    "entity_type": {"type": "string"},
                    "text": {"type": "string"},
                    "limit": {"type": "integer", "default": 20},
                },
            },
            **_READ_DEFAULTS,
        )
        async def onto_query(
            entity_type: str | None = None,
            text: str | None = None,
            limit: int = 20,
        ) -> list[dict[str, Any]]:
            return ontology.query_entities(
                entity_type=entity_type, text=text, limit=int(limit)
            )

        @server.register(
            name="onto_upsert",
            description="Insert or update one ontology entity",
            input_schema={
                "type": "object",
                "properties": {
                    "entity_type": {"type": "string"},
                    "name": {"type": "string"},
                    "canonical_key": {"type": "string"},
                    "attrs": {"type": "object"},
                },
                "required": ["entity_type", "name"],
            },
            **_LOCAL_WRITE_DEFAULTS,
        )
        async def onto_upsert(
            entity_type: str,
            name: str,
            canonical_key: str | None = None,
            attrs: dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            return ontology.upsert_entity(
                entity_type, name, canonical_key=canonical_key, attrs=attrs
            )

    # --------------------------------------------------------------- mail
    if mail_router is not None:

        @server.register(
            name="mail_search",
            description="Search a configured mailbox",
            input_schema={
                "type": "object",
                "properties": {
                    "account": {"type": "string"},
                    "query": {"type": "string"},
                    "folder": {"type": "string", "default": "INBOX"},
                    "limit": {"type": "integer", "default": 10},
                },
                "required": ["account", "query"],
            },
            **_READ_DEFAULTS,
        )
        async def mail_search(
            account: str, query: str, folder: str = "INBOX", limit: int = 10
        ) -> list[dict[str, Any]]:
            provider = mail_router.get_provider(account)
            return provider.search_messages(query, folder=folder, limit=int(limit))

        @server.register(
            name="mail_read_thread",
            description="Read one mail conversation",
            input_schema={
                "type": "object",
                "properties": {
                    "account": {"type": "string"},
                    "thread_id": {"type": "string"},
                },
                "required": ["account", "thread_id"],
            },
            **_READ_DEFAULTS,
        )
        async def mail_read_thread(account: str, thread_id: str) -> dict[str, Any]:
            return mail_router.get_provider(account).read_thread(thread_id)

        @server.register(
            name="mail_save_draft",
            description="Create a mail draft without sending it",
            input_schema={
                "type": "object",
                "properties": {
                    "account": {"type": "string"},
                    "to": {
                        "oneOf": [
                            {"type": "string"},
                            {"type": "array", "items": {"type": "string"}},
                        ]
                    },
                    "subject": {"type": "string"},
                    "body": {"type": "string"},
                    "cc": {
                        "oneOf": [
                            {"type": "string"},
                            {"type": "array", "items": {"type": "string"}},
                        ]
                    },
                    "reply_to_message_id": {"type": "string"},
                },
                "required": ["account", "to", "subject", "body"],
            },
            **_LOCAL_WRITE_DEFAULTS,
        )
        async def mail_save_draft(
            account: str,
            to: list[str] | str,
            subject: str,
            body: str,
            cc: list[str] | str | None = None,
            reply_to_message_id: str | None = None,
        ) -> dict[str, Any]:
            provider = mail_router.get_provider(account)
            return provider.save_draft(
                to=to,
                subject=subject,
                body=body,
                cc=cc,
                reply_to_message_id=reply_to_message_id,
            )

        @server.register(
            name="mail_send",
            description="Send a mail (external side effect; confirmation required)",
            input_schema={
                "type": "object",
                "properties": {
                    "account": {"type": "string"},
                    "to": {
                        "oneOf": [
                            {"type": "string"},
                            {"type": "array", "items": {"type": "string"}},
                        ]
                    },
                    "subject": {"type": "string"},
                    "body": {"type": "string"},
                    "cc": {
                        "oneOf": [
                            {"type": "string"},
                            {"type": "array", "items": {"type": "string"}},
                        ]
                    },
                    "reply_to_message_id": {"type": "string"},
                    "idempotency_key": {"type": "string"},
                },
                "required": ["account", "to", "subject", "body"],
            },
            **_EXTERNAL_WRITE_DEFAULTS,
        )
        async def mail_send(
            account: str,
            to: list[str] | str,
            subject: str,
            body: str,
            cc: list[str] | str | None = None,
            reply_to_message_id: str | None = None,
            idempotency_key: str | None = None,
        ) -> dict[str, Any]:
            provider = mail_router.get_provider(account)
            return provider.send_message(
                to=to,
                subject=subject,
                body=body,
                cc=cc,
                reply_to_message_id=reply_to_message_id,
                idempotency_key=idempotency_key,
            )

    # ------------------------------------------------------------- github
    if github_client is not None:

        @server.register(
            name="gh_list_notifications",
            description="List GitHub notifications",
            input_schema={
                "type": "object",
                "properties": {
                    "all_notifications": {"type": "boolean", "default": False},
                    "participating": {"type": "boolean", "default": False},
                    "limit": {"type": "integer", "default": 20},
                },
            },
            **_READ_DEFAULTS,
        )
        async def gh_list_notifications(
            all_notifications: bool = False,
            participating: bool = False,
            limit: int = 20,
        ) -> list[dict[str, Any]]:
            return github_client.list_notifications(
                all_notifications=bool(all_notifications),
                participating=bool(participating),
                limit=int(limit),
            )

        @server.register(
            name="gh_get_issue_or_pr",
            description="Fetch one GitHub issue or pull request",
            input_schema={
                "type": "object",
                "properties": {
                    "owner": {"type": "string"},
                    "repo": {"type": "string"},
                    "number": {"type": "integer"},
                },
                "required": ["owner", "repo", "number"],
            },
            **_READ_DEFAULTS,
        )
        async def gh_get_issue_or_pr(owner: str, repo: str, number: int) -> dict[str, Any]:
            return github_client.get_issue_or_pr(owner, repo, int(number))

        @server.register(
            name="gh_search_code",
            description="Search GitHub code, optionally scoped to one repository",
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "repo": {"type": "string", "description": "owner/name"},
                    "limit": {"type": "integer", "default": 10},
                },
                "required": ["query"],
            },
            **_READ_DEFAULTS,
        )
        async def gh_search_code(
            query: str, repo: str | None = None, limit: int = 10
        ) -> list[dict[str, Any]]:
            return github_client.search_code(query, repo=repo, limit=int(limit))

        @server.register(
            name="gh_create_issue",
            description="Create a GitHub issue (external side effect)",
            input_schema={
                "type": "object",
                "properties": {
                    "owner": {"type": "string"},
                    "repo": {"type": "string"},
                    "title": {"type": "string"},
                    "body": {"type": "string", "default": ""},
                    "labels": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["owner", "repo", "title"],
            },
            **_EXTERNAL_WRITE_DEFAULTS,
        )
        async def gh_create_issue(
            owner: str,
            repo: str,
            title: str,
            body: str = "",
            labels: list[str] | None = None,
        ) -> dict[str, Any]:
            return github_client.create_issue(
                owner=owner, repo=repo, title=title, body=body, labels=labels
            )

        @server.register(
            name="gh_post_comment",
            description="Comment on a GitHub issue or pull request (external side effect)",
            input_schema={
                "type": "object",
                "properties": {
                    "owner": {"type": "string"},
                    "repo": {"type": "string"},
                    "number": {"type": "integer"},
                    "body": {"type": "string"},
                },
                "required": ["owner", "repo", "number", "body"],
            },
            **_EXTERNAL_WRITE_DEFAULTS,
        )
        async def gh_post_comment(
            owner: str, repo: str, number: int, body: str
        ) -> dict[str, Any]:
            return github_client.post_comment(owner, repo, int(number), body)

    # ------------------------------------------------------------- feishu
    if feishu_client is not None:

        @server.register(
            name="feishu_list_chats",
            description="List Feishu chats visible to the tenant app",
            input_schema={
                "type": "object",
                "properties": {"page_size": {"type": "integer", "default": 20}},
            },
            **_READ_DEFAULTS,
        )
        async def feishu_list_chats(page_size: int = 20) -> list[dict[str, Any]]:
            return feishu_client.list_chats(page_size=int(page_size))

        @server.register(
            name="feishu_read_messages",
            description="Read Feishu messages from a chat or thread container",
            input_schema={
                "type": "object",
                "properties": {
                    "container_id": {"type": "string"},
                    "container_id_type": {"type": "string", "default": "chat"},
                    "page_size": {"type": "integer", "default": 20},
                },
                "required": ["container_id"],
            },
            **_READ_DEFAULTS,
        )
        async def feishu_read_messages(
            container_id: str,
            container_id_type: str = "chat",
            page_size: int = 20,
        ) -> list[dict[str, Any]]:
            return feishu_client.read_messages(
                container_id,
                container_id_type=container_id_type,
                page_size=int(page_size),
            )

        @server.register(
            name="feishu_read_doc",
            description="Read the plain-text content of a Feishu Docx document",
            input_schema={
                "type": "object",
                "properties": {"document_id": {"type": "string"}},
                "required": ["document_id"],
            },
            **_READ_DEFAULTS,
        )
        async def feishu_read_doc(document_id: str) -> dict[str, Any]:
            return feishu_client.read_doc(document_id)

        @server.register(
            name="feishu_send_message",
            description="Send a Feishu message (external side effect; confirmation required)",
            input_schema={
                "type": "object",
                "properties": {
                    "receive_id": {"type": "string"},
                    "receive_id_type": {
                        "type": "string",
                        "enum": ["open_id", "user_id", "union_id", "email", "chat_id"],
                    },
                    "msg_type": {"type": "string"},
                    "content": {
                        "oneOf": [{"type": "string"}, {"type": "object"}],
                    },
                    "uuid_key": {"type": "string"},
                },
                "required": ["receive_id", "receive_id_type", "msg_type", "content"],
            },
            **_EXTERNAL_WRITE_DEFAULTS,
        )
        async def feishu_send_message(
            receive_id: str,
            receive_id_type: str,
            msg_type: str,
            content: str | dict[str, Any],
            uuid_key: str | None = None,
        ) -> dict[str, Any]:
            key = None
            if uuid_key is not None:
                normalized = str(uuid_key).strip()
                # Feishu caps its idempotency uuid at 50 characters.
                key = normalized[:50] or None
            return feishu_client.send_message(
                receive_id,
                receive_id_type,
                msg_type,
                content,
                uuid_key=key,
            )

    return server


__all__ = ["register_copilot_tools"]
