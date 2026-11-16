"""Phase 8 §8.1: `knowledge_search` served over MCP (streamable HTTP).

Why this is the only tool that moves to MCP
-------------------------------------------
An MCP connection is long-lived and authenticated per *server*, not per turn:
it carries no per-user identity. The four READ tools that are bound to a user
(order / ticket / refund_evaluate / risk_check) depend on the per-turn identity
envelope, so moving them behind MCP would mean smuggling identity through the
channel — a security-model change, not a transport change. `knowledge_search`
searches a shared corpus with no per-user partitioning, so it can move without
touching the identity model. See the feasibility report in
`pi-harness/docs/mcp-feasibility.md` and `PHASE8_REPORT.md`.

The tool runtime is NOT the MCP SDK
-----------------------------------
The repository's in-process tool runtime used to be a top-level package named
`mcp/`, which shadowed the official MCP SDK for any process with the repository
root on `sys.path` — one name cannot be two packages in one process. It is now
`tool_runtime/`, so the SDK imports normally here and the gateway can also run
as a module:

    python internal_api/mcp_gateway.py          # from the repository root
    python -m internal_api.mcp_gateway          # equivalent

Auth: a static server-level bearer token (`SMARTCS_MCP_TOKEN`), never a user
identity. A missing or weak token is a startup failure — there is no default.
"""

from __future__ import annotations

import hmac
import json
import os
import pathlib
import sys
from typing import Any

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

MCP_TOKEN_ENV = "SMARTCS_MCP_TOKEN"
MCP_HOST_ENV = "SMARTCS_MCP_HOST"
MCP_PORT_ENV = "SMARTCS_MCP_PORT"
DEFAULT_PORT = 8972
MIN_TOKEN_BYTES = 16
#: Streamable HTTP endpoint FastMCP serves by default; the TS client appends it.
MCP_PATH = "/mcp"
#: Phase 11 readiness probe, served INSIDE the token guard (see HealthEndpoint).
HEALTH_PATH = "/health"


def _ensure_repo_importable() -> None:
    """Leave exactly the repository root as this package's import origin.

    Running this file directly puts `internal_api/` — not the repository root —
    at the front of `sys.path`, and then `internal_api/auth.py` shadows the
    top-level `auth/` package (`No module named 'auth.jwt'; 'auth' is not a
    package`). The directory is therefore removed and the root inserted, so
    every top-level name (`auth`, `memory`, `rag`, …) resolves to the real
    package.
    """
    own_dir = pathlib.Path(__file__).resolve().parent
    sys.path[:] = [entry for entry in sys.path if pathlib.Path(entry or ".").resolve() != own_dir]
    root = str(_REPO_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)


def _load_fastmcp():
    """Import FastMCP from the official MCP SDK (no local package shadows it)."""
    from mcp.server.fastmcp import FastMCP

    return FastMCP


def _load_rag_timing() -> Any:
    """The stdlib-only RAG timing module, imported normally."""
    from internal_api import rag_timing

    return rag_timing


def build_retriever() -> Any:
    """The same retriever the HTTP tool path uses (same env, same index)."""
    _ensure_repo_importable()
    from memory.knowledge import KnowledgeMemory

    memory = KnowledgeMemory(index_path=os.getenv("FAISS_INDEX_PATH", "./vector_store/faiss_index"))
    return memory.get_retriever()


def create_server(retriever: Any = None, *, token: str | None = None):
    """Build the FastMCP server exposing `knowledge_search`."""
    del token  # the token guards the transport, not the tool; see BearerTokenGuard
    _ensure_repo_importable()
    FastMCP = _load_fastmcp()
    shared_retriever = retriever if retriever is not None else build_retriever()
    # Phase 10 §⑤: optional segment timing. Off unless explicitly switched on,
    # and pass-through when on — it records durations, it never changes results.
    timing = _load_rag_timing()
    if timing is not None and timing.rag_timing_enabled():
        timing.install_retriever_timing(shared_retriever)
    server = FastMCP("smartcs-knowledge")

    @server.tool(
        name="knowledge_search",
        description="搜索企业知识库，返回相关文档片段",
    )
    async def knowledge_search(
        query: str,
        top_k: int = 3,
        domain: str | None = None,
        domains: list[str] | None = None,
    ) -> str:
        """Search the shared enterprise knowledge base (no per-user data)."""
        query = str(query).strip()
        if not query:
            raise ValueError("query must not be empty")
        selected_domains = domains or ([domain] if domain else None)
        results = shared_retriever.retrieve(
            query,
            domains=selected_domains,
            top_k=max(1, int(top_k)),
            rerank=True,
        )
        hits = [item.to_dict() if hasattr(item, "to_dict") else dict(item) for item in results]
        return json.dumps(hits, ensure_ascii=False)

    return server


def require_token() -> str:
    token = os.getenv(MCP_TOKEN_ENV, "")
    if len(token.encode("utf-8")) < MIN_TOKEN_BYTES or token != token.strip():
        raise ValueError(
            f"{MCP_TOKEN_ENV} must contain at least {MIN_TOKEN_BYTES} bytes without surrounding whitespace"
        )
    return token


class BearerTokenGuard:
    """Pure-ASGI guard: every HTTP request must carry the server token.

    Server-level, not user-level: this credential names the *caller service*
    (pi-harness), and no user identity travels through this channel at all.
    """

    def __init__(self, app: Any, token: str):
        self.app = app
        self._expected = f"Bearer {token}".encode("utf-8")

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        header = b""
        for name, value in scope.get("headers", []):
            if name == b"authorization":
                header = value.strip()
                break
        if not hmac.compare_digest(header, self._expected):
            body = json.dumps({"detail": "invalid MCP service token"}).encode("utf-8")
            await send({
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                    (b"cache-control", b"no-store"),
                ],
            })
            await send({"type": "http.response.body", "body": body})
            return
        await self.app(scope, receive, send)


class HealthEndpoint:
    """Answer `GET /health` for the harness's readiness check; delegate the rest.

    Mounted INSIDE `BearerTokenGuard` on purpose. A readiness answer the harness
    cannot distinguish from "wrong token" would be useless, and an unauthenticated
    health route would widen the gateway's public surface for no gain: with the
    guard in front, a 200 proves the token is right AND the ASGI app is up AND
    `create_server` finished — which is where the MCP server and the retriever
    are actually built. Nothing is searched, so the probe stays cheap.
    """

    def __init__(self, app: Any, *, ready: bool):
        self.app = app
        self._ready = bool(ready)

    async def __call__(self, scope, receive, send):
        if (
            scope["type"] == "http"
            and scope.get("path") == HEALTH_PATH
            and scope.get("method") == "GET"
        ):
            body = json.dumps(
                {"ok": self._ready, "server": "initialized", "retriever": "initialized" if self._ready else "unavailable"}
            ).encode("utf-8")
            await send({
                "type": "http.response.start",
                "status": 200 if self._ready else 503,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                    (b"cache-control", b"no-store"),
                ],
            })
            await send({"type": "http.response.body", "body": body})
            return
        await self.app(scope, receive, send)


def create_app(token: str | None = None, retriever: Any = None) -> Any:
    """The ASGI app: FastMCP's streamable HTTP app behind the token guard."""
    shared_retriever = retriever if retriever is not None else build_retriever()
    server = create_server(shared_retriever, token=token)
    # `shared_retriever` is the one `create_server` serves with, so this reflects
    # what would actually answer a search — not a second, separate probe.
    inner = HealthEndpoint(server.streamable_http_app(), ready=shared_retriever is not None)
    return BearerTokenGuard(inner, token or require_token())


def main() -> None:
    import uvicorn

    app = create_app()
    uvicorn.run(
        app,
        host=os.getenv(MCP_HOST_ENV, "127.0.0.1"),
        port=int(os.getenv(MCP_PORT_ENV, str(DEFAULT_PORT))),
        log_level=os.getenv("SMARTCS_MCP_LOG_LEVEL", "error"),
    )


if __name__ == "__main__":
    main()
