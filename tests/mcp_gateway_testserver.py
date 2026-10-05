"""Test launcher: the REAL MCP gateway over a known, isolated retriever.

Production builds its retriever from the RAG environment (see
`mcp_gateway.build_retriever`). Tests need a deterministic corpus instead, so
this launcher seeds the corpus in `mcp_test_corpus` and hands that retriever to
the same `create_app` the production entry point uses. The transport, the token
guard and the tool definition are the production ones — only the corpus differs.

Two import rules, both forced by the repository's own `mcp/` package:

* run this file directly (`python tests/mcp_gateway_testserver.py`) so
  `internal_api/__init__.py` never executes and never pulls the local package in;
* load `internal_api/mcp_gateway.py` by file path for the same reason.
"""

from __future__ import annotations

import importlib.util
import os
import pathlib
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(REPO_ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "tests"))


def _load_gateway_module():
    spec = importlib.util.spec_from_file_location(
        "smartcs_mcp_gateway_under_test", REPO_ROOT / "internal_api" / "mcp_gateway.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def main() -> None:
    import uvicorn

    gateway = _load_gateway_module()
    from tests.mcp_test_corpus import build_retriever

    app = gateway.create_app(gateway.require_token(), build_retriever())
    uvicorn.run(
        app,
        host=os.getenv("SMARTCS_MCP_HOST", "127.0.0.1"),
        port=int(os.getenv("SMARTCS_MCP_PORT", "8974")),
        log_level="error",
    )


if __name__ == "__main__":
    main()
