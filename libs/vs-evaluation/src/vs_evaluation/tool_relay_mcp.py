"""The tool server an agent runs for tools the host serves: it forwards, the host reads.

``relay_mcp_descriptor`` describes a stdio MCP server that holds no tool of its own. At start it
asks the host's ``ToolRelayService`` (``tool_relay_wire``) which tools exist and offers exactly
those; each call is forwarded to the host, and the host's text or error comes back. The server
imports only the standard library, pydantic, mcp and the framework's container packages, so it
starts on the agent container's interpreter, and what it forwards to never enters the container.

``python -m vs_evaluation.tool_relay_mcp --check`` is the startup check: it exits 0 when this
interpreter imports the server and the host's relay answers a ping on the socket, so a host can
run it in the agent's environment before the agent needs the tools and report why a tool server
would not start. It needs no token.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import anyio
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from vs_evaluation.tool_relay_wire import (
    RelayedTool,
    RelayRefusedError,
    RelayUnavailableError,
    ToolRelayClient,
)
from vs_mcp.api import StdioServerDescriptor, ToolServerDescriptor
from vs_sim.api import MonotonicClock, UnixNetwork

if TYPE_CHECKING:
    from collections.abc import Sequence

    from vs_sim.api import Clock, Network

SOCKET_ENV = "VS_TOOL_RELAY_SOCKET"
TOKEN_ENV = "VS_TOOL_RELAY_TOKEN"  # noqa: S105  # lint-waiver: LW-861903 [S105]; the name of the environment variable that carries the issued token, not a token.
_MODULE = "vs_evaluation.tool_relay_mcp"
_EXIT_FAILED = 1


class RelayedToolError(Exception):
    """A relayed tool failed on the host; the message is what it said, shown to the agent."""


def relay_mcp_descriptor(name: str, socket_path: str, token: str) -> ToolServerDescriptor:
    """Describe the tool server called ``name`` that forwards to the host relay at ``socket_path``.

    The interpreter and import roots are the launcher's to choose, as for the evaluation tool
    server. The socket and the token are endpoint and credential, so both ride the launch-only
    environment.
    """
    return StdioServerDescriptor(
        name=name,
        command="python",
        args=("-m", _MODULE, "--name", name),
        runtime_env=((SOCKET_ENV, socket_path), (TOKEN_ENV, token)),
    )


def relay_check_descriptor(name: str, socket_path: str) -> ToolServerDescriptor:
    """Describe the startup check of the server ``relay_mcp_descriptor`` describes.

    It has the server's name, so a failure names the server the agent lacks. It carries only the socket path: the check pings and needs no token, so no credential is
    put on a command line.
    """
    return StdioServerDescriptor(
        name=name,
        command="python",
        args=("-m", _MODULE, "--check"),
        runtime_env=((SOCKET_ENV, socket_path),),
    )


def build_relay_server(client: ToolRelayClient, tools: Sequence[RelayedTool], name: str) -> Server:
    """The MCP server offering ``tools`` and forwarding each call through ``client``."""
    offered = [
        types.Tool(name=tool.name, description=tool.description, inputSchema=tool.input_schema)
        for tool in tools
    ]
    server: Server = Server(name)

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return offered

    @server.call_tool()
    async def call_tool(tool: str, arguments: dict[str, Any]) -> list[types.TextContent]:
        # The call blocks this loop for its length: the server has one client and one
        # call in flight, and the host bounds the call.
        try:
            result = client.call(tool, arguments)
        except (RelayRefusedError, RelayUnavailableError) as error:
            raise RelayedToolError(str(error)) from error
        if result.is_error:
            raise RelayedToolError(result.text)
        return [types.TextContent(type="text", text=result.text)]

    return server


async def _serve(server: Server) -> None:
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def _client(*, with_token: bool, network: Network, clock: Clock) -> ToolRelayClient:
    needed = (SOCKET_ENV, TOKEN_ENV) if with_token else (SOCKET_ENV,)
    missing = [key for key in needed if key not in os.environ]
    if missing:
        message = f"the tool relay needs {', '.join(missing)} in its environment"
        raise SystemExit(message)
    return ToolRelayClient(
        Path(os.environ[SOCKET_ENV]),
        os.environ[TOKEN_ENV] if with_token else "",
        network=network,
        clock=clock,
    )


def main(argv: Sequence[str] | None = None) -> None:
    """Check the relay, or fetch its tools and serve them over stdio."""
    parser = argparse.ArgumentParser(prog=_MODULE)
    parser.add_argument("--name", default="vibesys", help="The MCP server name to offer.")
    parser.add_argument(
        "--check", action="store_true", help="Exit 0 when the host relay answers; serve nothing."
    )
    args = parser.parse_args(argv)
    network, clock = UnixNetwork(), MonotonicClock()
    try:
        if args.check:
            _client(with_token=False, network=network, clock=clock).ping()
            return
        client = _client(with_token=True, network=network, clock=clock)
        tools = client.list_tools()
    except (RelayRefusedError, RelayUnavailableError) as error:
        sys.stderr.write(f"{_MODULE}: {error}\n")
        raise SystemExit(_EXIT_FAILED) from error
    anyio.run(_serve, build_relay_server(client, tools, args.name))


if __name__ == "__main__":
    main()


__all__ = [
    "SOCKET_ENV",
    "TOKEN_ENV",
    "RelayedToolError",
    "build_relay_server",
    "main",
    "relay_check_descriptor",
    "relay_mcp_descriptor",
]
