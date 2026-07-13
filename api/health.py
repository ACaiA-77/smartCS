"""Health report helpers shared by the FastAPI endpoints and tests."""

from __future__ import annotations

from typing import Any


def build_readiness_report(
    *,
    graph_initialized: bool,
    short_term: dict[str, Any],
    long_term: dict[str, Any],
    require_redis: bool,
    require_rag_index: bool,
) -> dict[str, Any]:
    short_ready = bool(short_term.get("ready"))
    long_ready = bool(long_term.get("ready")) and int(long_term.get("document_count", 0)) > 0

    checks = {
        "graph": {
            "ready": graph_initialized,
            "required": True,
        },
        "short_term": {
            **short_term,
            "required": require_redis,
            "effective_ready": short_ready or not require_redis,
        },
        "long_term": {
            **long_term,
            "required": require_rag_index,
            "effective_ready": long_ready or not require_rag_index,
        },
    }
    ready = all(
        bool(check.get("effective_ready", check.get("ready")))
        for check in checks.values()
    )
    return {
        "status": "ready" if ready else "not_ready",
        "ready": ready,
        "checks": checks,
    }
