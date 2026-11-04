"""Every tool of every profiler MCP server refuses an argument it does not declare.

FastMCP's argument model drops unknown keys, so an agent's misspelled optional
argument would run the tool with the default and the agent would never learn
of the mistake. Each server passes its built server through the bundle's shared
``strict_arguments`` step; this test holds all nine to it, with synthesized calls.
"""

from __future__ import annotations

import asyncio
import functools
import importlib.util
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from mcp.server.fastmcp.exceptions import ToolError

from vs_sim.api.testing import examples

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP

_PROFILERS = Path(__file__).resolve().parents[3] / "resources" / "profilers"
#: Every profiler server in the bundle, so a new one cannot skip the rule.
_SERVERS = tuple(sorted(path.parent.name for path in _PROFILERS.glob("*/server.py")))


@functools.cache
def _build(profiler: str) -> FastMCP:
    """The server ``profiler``'s ``build_server`` returns, loaded by path as the agent runs it."""
    path = _PROFILERS / profiler / "server.py"
    spec = importlib.util.spec_from_file_location(f"_strict_{profiler}_server", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(path.parent))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(path.parent))
    return module.build_server()


_KEYS = st.text(alphabet="abcdefghijklmnopqrstuvwxyz_", min_size=1, max_size=12)


def test_the_bundle_has_profiler_servers() -> None:
    """Guards the glob: an empty match would make the property below vacuous."""
    assert len(_SERVERS) >= 9


@pytest.mark.parametrize("profiler", _SERVERS)
@settings(max_examples=examples(pr=3, nightly=50), deadline=None)
@given(key=_KEYS)
def test_a_tool_refuses_an_argument_it_does_not_declare(profiler: str, key: str) -> None:
    server = _build(profiler)
    tools = asyncio.run(server.list_tools())
    assert tools

    for tool in tools:
        declared = tool.inputSchema.get("properties", {})
        assert tool.inputSchema.get("additionalProperties") is False, tool.name
        undeclared = key if key not in declared else f"{key}_undeclared"
        with pytest.raises(ToolError, match=undeclared):
            asyncio.run(server.call_tool(tool.name, {undeclared: 1}))
