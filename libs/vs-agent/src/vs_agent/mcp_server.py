"""Generic FastMCP stdio runner for :class:`~vs_agent.tools.ToolSpec` lists.

Generalizes ``vs_issue_tracker.mcp``'s hand-written server: that module builds
one ``FastMCP`` instance and registers a fixed set of issue-tracker tools by
hand. This module does the same registration generically, driven by data
instead of a hardcoded tool list, and knows nothing about what any tool does.
"""

from __future__ import annotations

import inspect
from typing import TYPE_CHECKING

from mcp.server.fastmcp import FastMCP

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from typing import Any

    from pydantic import BaseModel

    from vs_agent.tools import ToolSpec


def _make_tool_function[T: BaseModel](spec: ToolSpec[T]) -> Callable[..., str]:
    """Build a plain function FastMCP can introspect for ``spec``'s flat schema.

    FastMCP derives a tool's JSON schema from the registered function's own
    signature (see ``mcp.server.fastmcp.utilities.func_metadata``), not from
    a nested ``BaseModel``-typed parameter: a function with a single
    ``args: SomeModel`` parameter produces a schema with one top-level
    ``"args"`` property wrapping the model, not the model's own fields at the
    top level. To get a flat schema matching ``spec.input_schema``'s fields,
    this builds a synthetic function whose ``__signature__`` mirrors those
    fields; FastMCP reads ``inspect.signature(fn)``, which honors an explicit
    ``__signature__`` override instead of re-deriving one from source. The
    wrapper then reassembles the keyword arguments into a
    ``spec.input_schema`` instance before calling ``spec.handler``. The
    override must also restate the ``-> str`` return annotation: FastMCP
    auto-detects a structured (dict-returning) tool from the return
    annotation it finds on this signature, not from the wrapper's own
    ``def`` line, and an empty return annotation silently downgrades the
    tool to unstructured, single-value output.
    """
    parameters = [
        inspect.Parameter(
            field_name,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            default=field.default if not field.is_required() else inspect.Parameter.empty,
            annotation=field.annotation,
        )
        for field_name, field in spec.input_schema.model_fields.items()
    ]

    def tool_function(**kwargs: object) -> str:
        args = spec.input_schema(**kwargs)
        return spec.handler(args)

    tool_function.__signature__ = inspect.Signature(  # ty: ignore[unresolved-attribute]
        parameters, return_annotation=str
    )
    tool_function.__name__ = spec.name
    tool_function.__doc__ = spec.description
    return tool_function


def register_tool[T: BaseModel](mcp: FastMCP, spec: ToolSpec[T]) -> None:
    """Register one :class:`ToolSpec` on ``mcp`` with a flattened argument schema."""
    mcp.add_tool(_make_tool_function(spec), name=spec.name, description=spec.description)


def serve_stdio(tools: Sequence[ToolSpec[Any]], *, server_name: str = "vibesys") -> None:
    """Run a stdio MCP server exposing ``tools``.

    Builds a :class:`FastMCP` instance, registers each tool programmatically,
    then serves over stdio until the client disconnects. Knows nothing about
    what the tools do; the caller assembles ``tools`` with handlers already
    bound to whatever backing state the subprocess opened for itself.
    """
    mcp = FastMCP(server_name)
    for spec in tools:
        register_tool(mcp, spec)
    mcp.run(transport="stdio")
