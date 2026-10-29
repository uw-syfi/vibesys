"""Check, before an agent needs them, that its tool servers can start in its container.

A tool server that cannot start (an import the container interpreter lacks, a socket that is
not mounted) shows the agent only as a connection that closed, and the agent can say no more
than that it could not read. Running the server's own startup check in the agent's
environment, the same interpreter and import roots the launcher gives the server, lets the host
report the cause instead.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from typing import TYPE_CHECKING

from vs_agent.container_python import containerize_server
from vs_agent.contracts import MCPServerSpec
from vs_sim.api import DISCARD

if TYPE_CHECKING:
    from collections.abc import Iterable

    from vs_mcp.api import ToolServerDescriptor
    from vs_sandbox.api import CommandRunner

CHECK_TIMEOUT_S = 60
"""How long one check may run before it counts as failed."""
_DETAIL_CHARS = 1000


@dataclass(frozen=True, slots=True)
class ToolServerFailure:
    """One check that failed: the check's name and what it printed, redacted and bounded."""

    server: str
    detail: str


def check_tool_servers(
    runner: CommandRunner,
    checks: Iterable[ToolServerDescriptor],
) -> tuple[ToolServerFailure, ...]:
    """Run each check in the container *runner* executes in; return those that did not exit 0.

    Each descriptor is made container-ready exactly as a tool server's is
    (:func:`~vs_agent.container_python.containerize_server`), so a check runs on the interpreter
    and import roots the server will. A check that times out or is cancelled has failed. Paths
    are not translated: the container interpreter and the framework mount are the same on both
    sides.
    """
    failures: list[ToolServerFailure] = []
    for descriptor in checks:
        spec = MCPServerSpec(
            descriptor.name,
            descriptor.command,
            descriptor.args,
            descriptor.env,
            descriptor.runtime_env,
        )
        server = containerize_server(spec)
        words = [f"{key}={value}" for key, value in (*server.env, *server.runtime_env)]
        command = shlex.join(["env", *words, server.command, *server.args])
        result = runner.execute(command, timeout=CHECK_TIMEOUT_S, output=DISCARD)
        if result.exit_code != 0 or result.cancelled:
            detail = result.diagnostic_output.strip()[-_DETAIL_CHARS:]
            failures.append(
                ToolServerFailure(descriptor.name, detail or f"exited with code {result.exit_code}")
            )
    return tuple(failures)


__all__ = ["CHECK_TIMEOUT_S", "ToolServerFailure", "check_tool_servers"]
