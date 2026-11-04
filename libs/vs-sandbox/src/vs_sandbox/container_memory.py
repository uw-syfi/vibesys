"""How much memory one sandbox container may use: the Docker host's memory less a reserve.

A container started without a limit may take every byte of the host. A candidate whose
build runs one large compiler per accelerator core in parallel did: four ``neuronx-cc``
backends took 127 of a host's 128 GiB, the kernel thrashed the whole host for 45 minutes
before its OOM killer acted, and the run's own process, starved with the rest, missed its
lease renewals and died. Capped below the host's memory, the runaway process is killed
inside its container, its command fails, and the host and the run go on.

The cap leaves a reserve for what runs beside the containers: the run's own process, the
agent CLIs' clients, the Docker daemon and the kernel. The host's memory is the daemon's
``MemTotal``, so the cap fits the host the containers actually run on.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from vs_sim.api import DISCARD, ProcessTimeoutError

if TYPE_CHECKING:
    from vs_sandbox.docker_cli import DockerCli

MEMORY_TOTAL_COMMAND = ("docker", "info", "--format", "{{.MemTotal}}")
"""The command whose output is the Docker host's memory, in bytes."""

_GIB = 1024**3
MIN_CONTAINER_MEMORY_RESERVE_BYTES = 8 * _GIB
"""The least memory the cap leaves to the host, whatever its size."""
RESERVE_DIVISOR = 8
"""On a large host the reserve is this fraction (one eighth) of its memory."""
_PROBE_TIMEOUT_S = 30


def container_memory_limit(total_bytes: int) -> int:
    """The bytes one container may use on a host with ``total_bytes`` of memory.

    The host keeps ``max(MIN_CONTAINER_MEMORY_RESERVE_BYTES, total_bytes // RESERVE_DIVISOR)``; on a host too
    small for that reserve, a container still gets half of the memory.
    """
    reserve = max(MIN_CONTAINER_MEMORY_RESERVE_BYTES, total_bytes // RESERVE_DIVISOR)
    return max(total_bytes - reserve, total_bytes // 2)


def docker_memory_total(docker: DockerCli) -> int | None:
    """The Docker host's memory in bytes, or ``None`` when the daemon does not report it."""
    try:
        result = docker.run(
            list(MEMORY_TOTAL_COMMAND), timeout_seconds=_PROBE_TIMEOUT_S, output=DISCARD
        )
    except (OSError, ProcessTimeoutError):
        return None
    reported = result.stdout.strip()
    if result.returncode != 0 or not reported.isdigit() or int(reported) <= 0:
        return None
    return int(reported)


class ContainerMemoryCap:
    """The cap every container a compute backend starts shares, asked of the Docker host once."""

    def __init__(self, docker: DockerCli) -> None:
        """Ask *docker* for the host's memory on the first :meth:`limit`."""
        self._docker = docker
        self._resolved = False
        self._limit: int | None = None

    def limit(self) -> int | None:
        """The bytes one container may use, or ``None`` when the host did not report its memory."""
        if not self._resolved:
            total = docker_memory_total(self._docker)
            self._limit = None if total is None else container_memory_limit(total)
            self._resolved = True
        return self._limit
