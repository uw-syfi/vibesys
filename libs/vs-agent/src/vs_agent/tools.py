"""Transport-neutral declarations for tools exposed to an agent turn.

Libraries describe subprocess-hosted tools through the structural
``ToolServerDescriptor`` contract. ``vs_agent`` owns the translation to the
transport understood by the selected driver.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

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


class ToolServerDescriptor(Protocol):
    """Structural description of a subprocess tool server.

    Libraries can provide this shape without depending on ``vs_agent``. The
    agent runtime maps it onto the tool transport supported by the selected
    driver.
    """

    @property
    def name(self) -> str:
        """Name used to identify the tool server."""
        ...

    @property
    def command(self) -> str:
        """Executable command for the subprocess."""
        ...

    @property
    def args(self) -> tuple[str, ...]:
        """Arguments passed to the subprocess command."""
        ...

    @property
    def env(self) -> tuple[tuple[str, str], ...]:
        """Environment variables passed to the subprocess."""
        ...


@dataclass(frozen=True, slots=True)
class StdioServerDescriptor:
    """Concrete descriptor produced by :func:`expose_as_tools`."""

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
) -> ToolServerDescriptor:
    """Build a descriptor for launching ``<command> -m <entrypoint_module> <entrypoint_args>``.

    Primitives only: ``entrypoint_module`` and ``entrypoint_args`` are what
    the subprocess's own CLI parses to rebuild its :class:`ToolSpec` list.
    No live object crosses the subprocess boundary.
    """
    return StdioServerDescriptor(
        name=name,
        command=command,
        args=("-m", entrypoint_module, *entrypoint_args),
        env=tuple(env.items()) if env else (),
    )
