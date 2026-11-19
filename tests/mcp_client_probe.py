"""MCP client probe used by tests/test_mcp_gateway.py.

Runs as its OWN process with a neutral cwd on purpose: the repository's
top-level `mcp/` package shadows the official SDK whenever the repository root
is on sys.path (see internal_api/mcp_gateway.py). Here the SDK must win.

Usage: python mcp_client_probe.py <url> <token> <command-json-file>
Prints one JSON object on stdout: {"ok": true, "result": ...} or
{"ok": false, "error": "...", "status": <http status if known>}.
"""

from __future__ import annotations

import asyncio
import json
import sys


async def run(url: str, token: str, command: dict) -> dict:
    from contextlib import AsyncExitStack
    from mcp import ClientSession
    import mcp.client.streamable_http as streamable_http

    async with AsyncExitStack() as stack:
        if hasattr(streamable_http, "streamablehttp_client"):
            read, write, _ = await stack.enter_async_context(
                streamable_http.streamablehttp_client(
                    url, headers={"Authorization": f"Bearer {token}"}
                )
            )
        else:
            http_client = await stack.enter_async_context(
                streamable_http.create_mcp_http_client(
                    headers={"Authorization": f"Bearer {token}"}
                )
            )
            read, write = await stack.enter_async_context(
                streamable_http.streamable_http_client(url, http_client=http_client)
            )
        session = await stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        if command["op"] == "list_tools":
            tools = await session.list_tools()
            return {
                "tools": [
                    {
                        "name": tool.name,
                        "description": tool.description,
                        "input_schema": (
                            tool.inputSchema
                            if hasattr(tool, "inputSchema")
                            else tool.input_schema
                        ),
                    }
                    for tool in tools.tools
                ]
            }
        if command["op"] == "call":
            result = await session.call_tool(command["name"], command.get("arguments", {}))
            texts = [block.text for block in result.content if getattr(block, "type", "") == "text"]
            is_error = getattr(result, "isError", getattr(result, "is_error", False))
            return {"isError": bool(is_error), "texts": texts}
        raise ValueError(f"unknown op: {command['op']}")


def main() -> None:
    url, token, command_file = sys.argv[1], sys.argv[2], sys.argv[3]
    command = json.loads(open(command_file, encoding="utf-8").read())
    try:
        result = asyncio.run(run(url, token, command))
        print(json.dumps({"ok": True, "result": result}, ensure_ascii=False))
    except Exception as exc:  # noqa: BLE001 - the probe reports, the test judges
        detail = str(exc) or type(exc).__name__
        print(json.dumps({"ok": False, "error": detail, "type": type(exc).__name__}, ensure_ascii=False))


if __name__ == "__main__":
    main()
