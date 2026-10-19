"""The host interfaces a run environment starts programs and serves sockets with."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from vs_sim.api import Network, PipedProcessLauncher, Processes, Threads


@dataclass(frozen=True, slots=True)
class EnvironmentSystem:
    """What a run environment reaches the host through; the entrypoint builds it.

    A test passes simulators and Fakes, so an environment that starts a host broker or
    an agent GPU command never falls back to the real operating system.
    """

    threads: Threads
    """Runs host brokers' accept loops and the agent GPU commands' output pumps."""
    network: Network
    """Binds the host brokers' sockets."""
    processes: Processes
    """Runs host programs to completion, such as the ``docker`` client."""
    launcher: PipedProcessLauncher
    """Starts streamed host programs, such as ``docker exec`` and an agent GPU command's ``srun``."""
    slurm_processes: Processes
    """Starts the Slurm programs (ssh, rsync, a connector, ``scancel``, ``squeue``)."""
    login_environment: Callable[[], Mapping[str, str]] | None = None
    """Reads the environment the user's login shell has, where tools such as ``cargo`` are found.

    A run started over a non-login ``ssh`` command or by the desktop helper has not
    sourced the rc files that put ``~/.cargo/bin`` on ``PATH``. The entrypoint passes the
    agent library's lookup (the same one that finds provider CLIs); it is read only when
    a tool must be installed. ``None`` means the process's own ``PATH`` is all there is.
    """

    def host_tool_search_path(self) -> str:
        """The login shell's ``PATH`` to search for build tools before the process's own, or empty."""
        if self.login_environment is None:
            return ""
        return self.login_environment().get("PATH", "")
