"""The profiler MCP server starts from the workspace of the editor container.

The server is the bundled support directory of the base's default profiler, mounted
into the workspace as the run mounts it and launched the way the profiler tool
binding describes it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest
from tests.minimal_container.conftest import expect_failure_for
from tests.minimal_container.stdio import StdioJsonProcess, mcp_initialize, mcp_tool_names

from launch.composition import AGENT_TOOL_BINDINGS
from vibesys.api.wiring import AgentToolContext
from vs_mcp.api import StdioServerDescriptor

if TYPE_CHECKING:
    from tests.minimal_container.editor import Editor

    from vs_runtime.api import AgentToolBindingContext

pytestmark = pytest.mark.minimal_container


def test_the_profiler_server_initializes_and_lists_tools(
    editor: Editor, request: pytest.FixtureRequest
) -> None:
    expect_failure_for(request, editor, ("cpu",), 1625)
    expect_failure_for(request, editor, ("rocm",), 1627)
    # The profiler binding reads only the context's profiler id, not the session binding.
    (descriptor,) = AGENT_TOOL_BINDINGS["profiler"](
        AgentToolContext(profiler_id=editor.profiler_support_name.removesuffix("_profiler")),
        cast("AgentToolBindingContext", None),
    )
    assert isinstance(descriptor, StdioServerDescriptor)

    with StdioJsonProcess(
        editor.argv([descriptor.command, *descriptor.args], env=descriptor.env)
    ) as server:
        initialized = mcp_initialize(server)
        tools = mcp_tool_names(server)

    assert initialized["serverInfo"]["name"]
    assert tools, "the profiler server offers no tools"
