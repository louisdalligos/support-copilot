"""The agent's access to the MCP servers, through LangChain tool objects.

Tools are loaded from each server with langchain-mcp-adapters, then called by
the graph's nodes with fixed arguments: the model never picks which tool to
call or with what. That keeps each step's tools narrow, and means text in a
ticket cannot talk the agent into calling a tool it was not going to call.

Servers run either as subprocesses over stdio (the CLI) or in-process over
memory streams (tests and evals, which need a fresh fake store per ticket).
"""

from __future__ import annotations

import os
import sys
from collections.abc import AsyncIterator, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any

from langchain_core.tools import BaseTool
from langchain_mcp_adapters.tools import load_mcp_tools
from mcp import ClientSession

SERVERS = ("zendesk", "shopify", "policies")


class ToolCallError(RuntimeError):
    def __init__(self, tool: str, message: str) -> None:
        super().__init__(f"{tool}: {message}")
        self.tool = tool
        self.message = message


class StoreTools:
    """Calls MCP tools by name and returns their structured results."""

    def __init__(self, tools: dict[str, BaseTool]) -> None:
        self.tools = tools
        self.calls: list[dict[str, Any]] = []  # a trace of every call, for the run record and evals

    async def call(self, name: str, **args: Any) -> Any:
        tool = self.tools[name]
        message = await tool.ainvoke(
            {"type": "tool_call", "id": f"call-{len(self.calls) + 1}", "name": name, "args": args}
        )
        ok = message.status != "error"
        self.calls.append({"tool": name, "args": args, "ok": ok})
        if not ok:
            raise ToolCallError(name, _text(message.content))
        artifact = message.artifact or {}
        data = artifact.get("structured_content") if isinstance(artifact, dict) else None
        if data is None:
            raise ToolCallError(name, "tool returned no structured result")
        return data["result"] if set(data) == {"result"} else data

    async def try_call(self, name: str, **args: Any) -> Any | None:
        """Like call, but a tool error (order not found, and so on) returns None."""
        try:
            return await self.call(name, **args)
        except ToolCallError:
            return None


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    return " ".join(block.get("text", "") for block in content if isinstance(block, dict))


async def load_tools(sessions: dict[str, ClientSession]) -> StoreTools:
    tools: dict[str, BaseTool] = {}
    for session in sessions.values():
        for tool in await load_mcp_tools(session):
            tools[tool.name] = tool
    return StoreTools(tools)


def stdio_connections(rag_models: bool = False) -> dict[str, dict[str, Any]]:
    """How to start each server as a subprocess, for langchain-mcp-adapters."""
    policies_args = ["-m", "copilot.rag"] + (["--dense", "--rerank"] if rag_models else [])
    # Pass the environment through: the MCP client otherwise starts servers with a minimal
    # one, and the policy server needs HF_TOKEN (and the venv's settings) for its models.
    env = dict(os.environ)

    def server(*args: str) -> dict[str, Any]:
        return {"transport": "stdio", "command": sys.executable, "args": list(args), "env": env}

    return {
        "zendesk": server("-m", "copilot.zendesk"),
        "shopify": server("-m", "copilot.shopify"),
        "policies": server(*policies_args),
    }


@asynccontextmanager
async def stdio_tools(rag_models: bool = False) -> AsyncIterator[StoreTools]:
    """Start the three servers as subprocesses and connect to them."""
    from langchain_mcp_adapters.client import MultiServerMCPClient

    client = MultiServerMCPClient(stdio_connections(rag_models))
    async with AsyncExitStack() as stack:
        sessions = {name: await stack.enter_async_context(client.session(name)) for name in SERVERS}
        yield await load_tools(sessions)


@asynccontextmanager
async def in_memory_tools(servers: dict[str, Callable[[], Any]]) -> AsyncIterator[StoreTools]:
    """Connect to FastMCP servers in this process. `servers` maps a name to a server factory."""
    from mcp.shared.memory import create_connected_server_and_client_session

    async with AsyncExitStack() as stack:
        sessions = {
            name: await stack.enter_async_context(create_connected_server_and_client_session(make()))
            for name, make in servers.items()
        }
        yield await load_tools(sessions)
