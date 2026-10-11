"""The profiler MCP server starts from the workspace of the editor container.

The server is the bundled support directory of the base's default profiler, mounted
into the workspace as the run mounts it and launched the way the profiler tool
binding describes it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from tests.minimal_container.stdio import StdioJsonProcess, mcp_initialize, mcp_tool_names

from launch.composition import profiler_server
from vibesys.api.wiring import AgentToolContext
from vs_mcp.api import StdioServerDescriptor

if TYPE_CHECKING:
    from tests.minimal_container.editor import Editor

pytestmark = pytest.mark.minimal_container

#: Also run on the stand-in bases: an Ubuntu base has no `python` of its own.
INCLUDE_STAND_INS = True


def test_the_profiler_server_initializes_and_lists_tools(editor: Editor) -> None:
    descriptor = profiler_server(
        AgentToolContext(profiler_id=editor.profiler_support_name.removesuffix("_profiler"))
    )
    assert isinstance(descriptor, StdioServerDescriptor)

    with StdioJsonProcess(editor.server_argv(descriptor)) as server:
        initialized = mcp_initialize(server)
        tools = mcp_tool_names(server)

    assert initialized["serverInfo"]["name"]
    assert tools, "the profiler server offers no tools"
