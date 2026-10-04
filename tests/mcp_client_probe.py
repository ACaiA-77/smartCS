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
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    async with streamablehttp_client(url, headers={"Authorization": f"Bearer {token}"}) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            if command["op"] == "list_tools":
                tools = await session.list_tools()
                return {
                    "tools": [
                        {"name": tool.name, "description": tool.description,
                         "input_schema": tool.inputSchema}
                        for tool in tools.tools
                    ]
                }
            if command["op"] == "call":
                result = await session.call_tool(command["name"], command.get("arguments", {}))
                texts = [block.text for block in result.content if getattr(block, "type", "") == "text"]
                return {"isError": bool(result.isError), "texts": texts}
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
