"""Pure descriptors for exposing handlers as a stdio MCP tool server.

``vs_agent`` never launches a subprocess or builds a core MCP server spec
itself: it only describes what a subprocess-hosted server would look like.
A vibesys-owned module (the analogue of
``vibesys.loops.plain.mcp_config.build_issue_mcp_spec``) turns a
:class:`StdioServerDescriptor` into the actual
``vibesys.agents.contracts.MCPServerSpec`` that a driver launches. This
module does not import that type.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from pydantic import BaseModel


@dataclass(frozen=True, slots=True)
class ToolSpec[T: BaseModel]:
    """One tool a :func:`vs_agent.mcp_server.serve_stdio` server registers.

    ``handler`` is only ever built and invoked inside the subprocess that
    runs the server, so it may close over live objects (an open store, a
    read-model) that must not cross the subprocess boundary. It receives one
    validated ``input_schema`` instance and returns the tool result text.

    Generic over the concrete ``input_schema``/``handler`` argument type ``T``
    so each construction site keeps its handler's real parameter type (a
    ``BaseModel`` subclass) instead of widening it to ``BaseModel``, which
    would let a handler for one tool's schema be paired with another's.
    """

    name: str
    description: str
    input_schema: type[T]
    handler: Callable[[T], str]


@dataclass(frozen=True, slots=True)
class StdioServerDescriptor:
    """A stdio MCP server launch, shaped to map 1:1 onto ``MCPServerSpec``.

    Pure data: no live objects, no dependency on
    ``vibesys.agents.contracts``. A vibesys adapter reads these fields to
    build the actual spec.
    """

    name: str
    command: str
    args: tuple[str, ...] = ()
    env: tuple[tuple[str, str], ...] = ()


def expose_as_tools(
    *,
    name: str,
    entrypoint_module: str,
    entrypoint_args: Sequence[str] = (),
    env: Mapping[str, str] | None = None,
    command: str = "python",
) -> StdioServerDescriptor:
    """Build a descriptor for launching ``<command> -m <entrypoint_module> <entrypoint_args>``.

    Primitives only: ``entrypoint_module`` and ``entrypoint_args`` are what
    the subprocess's own CLI parses to rebuild its :class:`ToolSpec` list
    from scratch, mirroring ``build_issue_mcp_spec``'s primitive-argv
    approach. No live object crosses the subprocess boundary.
    """
    return StdioServerDescriptor(
        name=name,
        command=command,
        args=("-m", entrypoint_module, *entrypoint_args),
        env=tuple(env.items()) if env else (),
    )
