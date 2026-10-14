"""Make every tool of a profiler MCP server refuse arguments it does not declare.

FastMCP validates a call's arguments with a model built from the handler's
signature, and that model drops a key it does not know: an agent's misspelled
optional argument (``top_n`` for ``limit``) would run the tool with the default
instead, and the agent would never learn it. Each profiler server passes its
built server through :func:`strict_arguments`, so every registered tool
validates with a subclass of that model that forbids extra keys and advertises
``additionalProperties: false``. This is the rule ``vs_mcp`` applies to its own
tools; profiler servers run on the agent's interpreter, which need not have
``vs_mcp``, so the bundle carries the same step.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import ConfigDict

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP


def strict_arguments(mcp: FastMCP) -> FastMCP:
    """``mcp``, with every tool registered so far refusing undeclared argument keys."""
    # lint-waiver: LW-960302 [SLF001]; FastMCP exposes registered tools only as wire schemas.
    # > Its public surface (list_tools) returns wire descriptions, not the Tool objects
    # > whose argument model validates calls; re-registering every handler through a
    # > wrapper decorator would need each of nine servers to change how it registers.
    for tool in mcp._tool_manager.list_tools():  # noqa: SLF001
        loose = tool.fn_metadata.arg_model
        # Pydantic merges a subclass's config into its parent's, so only ``extra`` changes.
        strict = type(loose.__name__, (loose,), {"model_config": ConfigDict(extra="forbid")})
        tool.fn_metadata = tool.fn_metadata.model_copy(update={"arg_model": strict})
        tool.parameters = strict.model_json_schema(by_alias=True)
    return mcp
