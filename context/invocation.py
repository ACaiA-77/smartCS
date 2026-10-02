"""Shared invocation entry point for SmartCS LLM calls."""

from __future__ import annotations

from typing import Any

from context.manager import ContextManager, active_context


_default_context_manager: ContextManager | None = None


def configure_context_manager(manager: ContextManager | None) -> None:
    """Set the application-owned manager used by direct Agent calls."""
    global _default_context_manager
    _default_context_manager = manager


def get_context_manager() -> ContextManager:
    """Return the active request manager or an explicitly ephemeral fallback."""
    active = active_context.get()
    if active is not None and isinstance(active.manager, ContextManager):
        return active.manager
    global _default_context_manager
    if _default_context_manager is None:
        _default_context_manager = ContextManager()
    return _default_context_manager


def _model_name(llm: Any) -> str | None:
    value = getattr(llm, "model_name", None) or getattr(llm, "model", None)
    return value if isinstance(value, str) else None


async def invoke_agent(
    llm: Any,
    agent: str,
    messages: list[Any],
    *,
    state: dict[str, Any] | None = None,
    task_message: str | None = None,
    evidence: Any | None = None,
    tool_schemas: Any | None = None,
    isolated: bool = False,
) -> Any:
    """Build centrally-budgeted context, then invoke the model exactly once.

    Direct Agent usage shares the same policy and budget builder but falls back
    to an in-memory, non-durable manager. Sensitive single-purpose calls may opt
    into ``isolated`` to avoid attaching unrelated session history or memory.
    """
    active = active_context.get()
    model = _model_name(llm)

    if isolated:
        manager = (
            active.manager
            if active is not None and isinstance(active.manager, ContextManager)
            else get_context_manager()
        )
        if active is not None:
            # Keep request identity/provenance while the agent policy isolates
            # prompt data. Query-only policies skip storage and dynamic blocks.
            return await manager.invoke(
                llm,
                agent,
                messages,
                state=state,
                session_id=active.session_id,
                user_id=active.user_id,
                model=model,
                task_message=task_message,
                evidence=evidence,
                tool_schemas=tool_schemas,
            )
        # Standalone isolated calls retain the non-durable ephemeral fallback.
        return await manager.invoke(
            llm,
            agent,
            messages,
            state=None,
            model=model,
            task_message=task_message,
            evidence=evidence,
            tool_schemas=tool_schemas,
        )

    manager = (
        active.manager
        if active is not None and isinstance(active.manager, ContextManager)
        else get_context_manager()
    )
    if active is not None and active.manager is manager:
        if state is not None:
            active.state = state
        return await manager.invoke(
            llm,
            agent,
            messages,
            state=state,
            session_id=active.session_id,
            user_id=active.user_id,
            model=model,
            task_message=task_message,
            evidence=evidence,
            tool_schemas=tool_schemas,
        )

    return await manager.invoke(
        llm,
        agent,
        messages,
        state=state,
        session_id=(state or {}).get("session_id"),
        user_id=(state or {}).get("user_id"),
        model=model,
        task_message=task_message,
        evidence=evidence,
        tool_schemas=tool_schemas,
    )


__all__ = ["configure_context_manager", "get_context_manager", "invoke_agent"]
