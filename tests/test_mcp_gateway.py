"""Phase 8 §8.1 (P8-1/P8-2): `knowledge_search` over MCP.

The gateway runs as its own process — that is not a test shortcut but the
production shape: the repository's own `mcp/` package and the official SDK
cannot share one process (see internal_api/mcp_gateway.py). The MCP client also
runs as its own process with a neutral cwd, for the same reason.

Parity (P8-1) is asserted against the HTTP path's retriever directly: both
transports are handed the SAME isolated corpus, so the only variable left is
the transport itself.
"""

from __future__ import annotations

import json
import os
import pathlib
import socket
import subprocess
import sys
import tempfile
import time

import pytest

PYTHON_IMPL = pathlib.Path(__file__).resolve().parents[1]
LAUNCHER = PYTHON_IMPL / "tests" / "mcp_gateway_testserver.py"
PROBE = PYTHON_IMPL / "tests" / "mcp_client_probe.py"
TOKEN = "phase8-mcp-test-token-0123456789"
WRONG_TOKEN = "phase8-mcp-wrong-token-000000000"
QUERY = "退款政策"


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class _Gateway:
    """The real gateway process, on a known corpus."""

    def __init__(self, port: int, child: subprocess.Popen):
        self.port = port
        self.child = child

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/mcp"

    def stop(self) -> None:
        if self.child.poll() is None:
            self.child.kill()
        try:
            self.child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass


@pytest.fixture
def gateway():
    port = _free_port()
    env = {
        **os.environ,
        "PYTHONPATH": str(PYTHON_IMPL) + os.pathsep + str(PYTHON_IMPL / "tests"),
        "SMARTCS_MCP_TOKEN": TOKEN,
        "SMARTCS_MCP_PORT": str(port),
    }
    child = subprocess.Popen(
        [sys.executable, str(LAUNCHER)], env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    handle = _Gateway(port, child)
    deadline = time.time() + 120
    while time.time() < deadline:
        if child.poll() is not None:
            raise RuntimeError(f"gateway exited during startup: {child.stderr.read()[-800:] if child.stderr else ''}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                break
        except OSError:
            time.sleep(0.3)
    else:
        handle.stop()
        raise RuntimeError("gateway did not start")
    yield handle
    handle.stop()


def _mcp_call(url: str, token: str, payload: dict) -> dict:
    """Drive the real MCP client, from a neutral cwd (SDK importable there)."""
    with tempfile.TemporaryDirectory() as workdir:
        command = pathlib.Path(workdir) / "command.json"
        command.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        result = subprocess.run(
            [sys.executable, str(PROBE), url, token, str(command)],
            cwd=workdir, env=env, capture_output=True, text=True, timeout=240)
    output = result.stdout.strip().splitlines()
    if not output:
        raise AssertionError(f"probe produced no output: {result.stderr[-500:]}")
    return json.loads(output[-1])


async def _http_path_hits(query: str, top_k: int = 3) -> list[dict]:
    """The HTTP tool path's own answer, over the same corpus."""
    from mcp.mcp_server import MCPToolServer, create_default_tools  # the repository's own tool server
    from tests.mcp_test_corpus import build_memory

    server = create_default_tools(MCPToolServer(), long_term_memory=build_memory())
    result = await server.call_tool("knowledge_search", {"query": query, "top_k": top_k})
    assert result.success is True, result.error
    return result.result


@pytest.mark.asyncio
async def test_tools_list_exposes_exactly_knowledge_search(gateway):
    """P8-2 tool face: the MCP channel adds no tool the HTTP path did not have."""
    response = _mcp_call(gateway.url, TOKEN, {"op": "list_tools"})
    assert response["ok"] is True, response
    tools = response["result"]["tools"]
    assert [tool["name"] for tool in tools] == ["knowledge_search"]
    schema = tools[0]["input_schema"]
    assert set(schema["properties"]) == {"query", "top_k", "domain", "domains"}
    # No user identity is expressible through this channel.
    for forbidden in ("user_id", "business_user_id", "account_id"):
        assert forbidden not in schema["properties"]


@pytest.mark.asyncio
async def test_mcp_results_match_the_http_path(gateway):
    """P8-1: same query, same corpus -> identical evidence on both transports."""
    http_hits = await _http_path_hits(QUERY, top_k=2)
    assert http_hits, "the isolated corpus must produce hits, or parity is vacuous"

    response = _mcp_call(gateway.url, TOKEN,
                         {"op": "call", "name": "knowledge_search", "arguments": {"query": QUERY, "top_k": 2}})
    assert response["ok"] is True, response
    assert response["result"]["isError"] is False
    mcp_hits = json.loads(response["result"]["texts"][0])

    assert [hit["source"] for hit in mcp_hits] == [hit["source"] for hit in http_hits]
    assert [hit["content"] for hit in mcp_hits] == [hit["content"] for hit in http_hits]
    assert [hit["score"] for hit in mcp_hits] == [hit["score"] for hit in http_hits]


@pytest.mark.asyncio
@pytest.mark.parametrize("token", [WRONG_TOKEN, ""], ids=["wrong", "absent"])
async def test_a_bad_service_token_cannot_connect(gateway, token):
    response = _mcp_call(gateway.url, token, {"op": "list_tools"})
    assert response["ok"] is False
    assert "Group" in response["type"] or "error" in response  # connection refused at the handshake


@pytest.mark.asyncio
async def test_an_unset_token_refuses_to_start():
    """No token, no gateway: there is deliberately no default credential."""
    port = _free_port()
    env = {**os.environ, "PYTHONPATH": str(PYTHON_IMPL) + os.pathsep + str(PYTHON_IMPL / "tests"), "SMARTCS_MCP_PORT": str(port)}
    env.pop("SMARTCS_MCP_TOKEN", None)
    result = subprocess.run([sys.executable, str(LAUNCHER)], env=env,
                            capture_output=True, text=True, timeout=180)
    assert result.returncode != 0
    assert "SMARTCS_MCP_TOKEN" in result.stderr
