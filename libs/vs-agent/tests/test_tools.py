"""Tests for the generic subprocess-hosted MCP tool bridge.

We test registration and the flattened argument schema directly through
``register_tool`` and ``FastMCP.call_tool``/``list_tools``, the same way
``vs_issue_board``'s tests exercise its hand-written server. We do not call
``serve_stdio`` (it blocks on the stdio JSON-RPC loop, which belongs to the
``mcp`` package's own test suite, not ours).
"""

from __future__ import annotations

import asyncio
import dataclasses
from typing import Any, cast

import pytest
from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel

from vs_agent.mcp_server import register_tool
from vs_agent.tools import StdioServerDescriptor, ToolSpec, expose_as_tools


class _EchoArgs(BaseModel):
    text: str
    shout: bool = False


def _echo_handler(args: BaseModel) -> str:
    assert isinstance(args, _EchoArgs)
    return args.text.upper() if args.shout else args.text


def _shout_handler(args: BaseModel) -> str:
    assert isinstance(args, _EchoArgs)
    return args.text.upper()


async def _list_tool_names(server: FastMCP) -> set[str]:
    tools = await server.list_tools()
    return {t.name for t in tools}


async def _call_tool(server: FastMCP, name: str, **kwargs: object) -> str:
    """Invoke an MCP tool and return its string result.

    ``FastMCP.call_tool`` is declared as returning
    ``Sequence[ContentBlock] | dict[str, Any]``, but for a tool with an
    output schema (every tool here, since each returns plain ``str``) its
    implementation (``FuncMetadata.convert_result``) actually returns a
    ``(content_blocks, structured_dict)`` tuple; the declared return type
    just doesn't reflect that case. The cast documents the real, narrower
    shape instead of widening the result to ``Any``.
    """
    _, structured = cast("tuple[object, dict[str, Any]]", await server.call_tool(name, kwargs))
    return structured["result"]


# ---------------------------------------------------------------------------
# expose_as_tools
# ---------------------------------------------------------------------------


class TestExposeAsTools:
    def test_builds_module_invocation_args(self) -> None:
        descriptor = expose_as_tools(name="vibesys-thing", entrypoint_module="vs_thing.mcp")

        assert descriptor == StdioServerDescriptor(
            name="vibesys-thing",
            command="python",
            args=("-m", "vs_thing.mcp"),
            env=(),
        )

    def test_appends_entrypoint_args_after_the_module(self) -> None:
        descriptor = expose_as_tools(
            name="vibesys-thing",
            entrypoint_module="vs_thing.mcp",
            entrypoint_args=["store.json", "--read-only"],
        )

        assert descriptor.args == ("-m", "vs_thing.mcp", "store.json", "--read-only")

    def test_converts_env_mapping_to_a_tuple_of_pairs(self) -> None:
        descriptor = expose_as_tools(
            name="vibesys-thing",
            entrypoint_module="vs_thing.mcp",
            env={"VIBESYS_RUN_ID": "run-1"},
        )

        assert descriptor.env == (("VIBESYS_RUN_ID", "run-1"),)

    def test_omitted_env_defaults_to_empty_tuple(self) -> None:
        descriptor = expose_as_tools(name="vibesys-thing", entrypoint_module="vs_thing.mcp")

        assert descriptor.env == ()

    def test_command_is_overridable(self) -> None:
        descriptor = expose_as_tools(
            name="vibesys-thing", entrypoint_module="vs_thing.mcp", command="python3"
        )

        assert descriptor.command == "python3"


# ---------------------------------------------------------------------------
# ToolSpec / StdioServerDescriptor are pure immutable values
# ---------------------------------------------------------------------------


class TestValueSemantics:
    def test_tool_spec_is_frozen(self) -> None:
        spec = ToolSpec(
            name="echo",
            description="Echo back text.",
            input_schema=_EchoArgs,
            handler=_echo_handler,
        )

        with pytest.raises(dataclasses.FrozenInstanceError):
            spec.name = "renamed"  # ty: ignore[invalid-assignment]

    def test_stdio_server_descriptor_is_frozen(self) -> None:
        descriptor = StdioServerDescriptor(name="vibesys-thing", command="python")

        with pytest.raises(dataclasses.FrozenInstanceError):
            descriptor.command = "python3"  # ty: ignore[invalid-assignment]

    def test_equal_tool_specs_compare_equal(self) -> None:
        first = ToolSpec(
            name="echo", description="d", input_schema=_EchoArgs, handler=_echo_handler
        )
        second = ToolSpec(
            name="echo", description="d", input_schema=_EchoArgs, handler=_echo_handler
        )

        assert first == second


# ---------------------------------------------------------------------------
# register_tool: flattened schema and handler round-trip via a real FastMCP
# ---------------------------------------------------------------------------


class TestRegisterTool:
    def test_registers_the_tool_by_name(self) -> None:
        mcp = FastMCP("test")
        register_tool(
            mcp,
            ToolSpec(
                name="echo",
                description="Echo back text.",
                input_schema=_EchoArgs,
                handler=_echo_handler,
            ),
        )

        names = asyncio.run(_list_tool_names(mcp))
        assert names == {"echo"}

    def test_input_schema_is_flattened_not_nested_under_a_single_field(self) -> None:
        mcp = FastMCP("test")
        register_tool(
            mcp,
            ToolSpec(
                name="echo",
                description="Echo back text.",
                input_schema=_EchoArgs,
                handler=_echo_handler,
            ),
        )

        tools = asyncio.run(mcp.list_tools())
        (tool,) = [t for t in tools if t.name == "echo"]
        assert set(tool.inputSchema["properties"]) == {"text", "shout"}
        assert tool.inputSchema["required"] == ["text"]

    def test_handler_round_trips_through_call_tool(self) -> None:
        mcp = FastMCP("test")
        register_tool(
            mcp,
            ToolSpec(
                name="echo",
                description="Echo back text.",
                input_schema=_EchoArgs,
                handler=_echo_handler,
            ),
        )

        out = asyncio.run(_call_tool(mcp, "echo", text="hi", shout=True))
        assert out == "HI"

    def test_handler_receives_the_input_schema_default(self) -> None:
        mcp = FastMCP("test")
        register_tool(
            mcp,
            ToolSpec(
                name="echo",
                description="Echo back text.",
                input_schema=_EchoArgs,
                handler=_echo_handler,
            ),
        )

        out = asyncio.run(_call_tool(mcp, "echo", text="hi"))
        assert out == "hi"

    def test_multiple_tools_register_independently(self) -> None:
        mcp = FastMCP("test")
        register_tool(
            mcp,
            ToolSpec(
                name="echo",
                description="Echo back text.",
                input_schema=_EchoArgs,
                handler=_echo_handler,
            ),
        )
        register_tool(
            mcp,
            ToolSpec(
                name="shout",
                description="Always shout.",
                input_schema=_EchoArgs,
                handler=_shout_handler,
            ),
        )

        names = asyncio.run(_list_tool_names(mcp))
        assert names == {"echo", "shout"}
