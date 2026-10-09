"""A started Docker sandbox as an ``agentshim.Confinement``.

agentshim owns how a confined process is launched and found again:
``docker exec`` with the environment passed by name, an ``AGENTSHIM_CONFINED``
marker on every process it starts, and ``reap`` to kill the marked ones. This
module only supplies what VibeSys decides: which container (read at every call,
because a GPU reselect replaces it) and how host paths appear inside it (the
sandbox's own resource mapping).
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import agentshim

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from vs_sandbox.api import DockerSandbox


class DockerContainerConfinement:
    """Run agent processes with ``docker exec`` in a sandbox's container.

    The sandbox must already be started: its environment (``HOME`` and the
    container's ``PATH`` among it) is read once, here. The environment travels
    by name, so a credential never appears in the host process table.
    """

    def __init__(self, sandbox: DockerSandbox, *, runner: agentshim.CommandExecutor) -> None:
        """Bind *sandbox*; *runner* executes the ``docker`` client for ``reap``."""
        self._sandbox = sandbox
        self._exec = agentshim.DockerExecConfinement(
            lambda: sandbox.container_id, runner=runner, env=sandbox.env
        )

    @property
    def env(self) -> Mapping[str, str]:
        """The environment the confined process gets."""
        return self._exec.env

    def agent_path(self, host_path: str | os.PathLike[str]) -> str:
        """Return the path the container sees for *host_path*."""
        return self._sandbox.agent_path(os.fspath(host_path))

    def wrap(self, argv: Sequence[str], cwd: str | None) -> list[str]:
        """Return the ``docker exec`` argv for *argv*; *cwd* is already the container's view."""
        return self._exec.wrap(argv, cwd)

    def reap(self) -> None:
        """Kill every marked process in the sandbox's container."""
        self._exec.reap()
