"""Helpers for testing MCP servers through a real client session held in memory."""

from __future__ import annotations

import io
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import anyio
from mcp.client.session import ClientSession
from mcp.server.fastmcp import FastMCP
from mcp.shared.memory import create_connected_server_and_client_session

from copilot.audit import AuditLog

DATA = Path(__file__).resolve().parents[1] / "data" / "generated"

Test = Callable[[ClientSession], Awaitable[None]]


def run(make_server: Callable[[AuditLog], FastMCP], test: Test, name: str = "test") -> AuditLog:
    """Build a fresh server, connect a client to it and run the test. Returns the audit log."""
    audit = AuditLog(name, stream=io.StringIO())
    server = make_server(audit)

    async def main() -> None:
        async with create_connected_server_and_client_session(server) as client:
            await test(client)

    anyio.run(main)
    return audit


def payload(result: Any) -> Any:
    """The value a tool returned, from its structured content or its JSON text."""
    assert not result.isError, result.content
    if result.structuredContent is not None:
        data = result.structuredContent
        return data["result"] if set(data) == {"result"} else data
    return json.loads(result.content[0].text)


def error_text(result: Any) -> str:
    assert result.isError, "expected the tool call to fail"
    return result.content[0].text
