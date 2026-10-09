"""Find and end the agents a dead host process left in Docker containers.

A run's containers carry the ``vibesys.run-id`` label. When a run is resumed, the
process that started them is gone (the resume holds the run's exclusive host lock),
so every container still labelled with the run id is an orphan, and every agent
process in it is one the new host did not start and cannot talk to. Resuming without
ending them would leave a second agent writing in the same workspace.

``reap_orphaned_agents`` ends them in two steps per container: agentshim's
``Confinement.reap()`` kills the marked agent processes (so a still-running turn
stops writing before anything else happens), then ``docker rm -f`` removes the
container. A step that fails raises :class:`OrphanReapError` naming the container:
the run must not resume while an agent may still be running.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

import agentshim

from vs_sandbox.api import RUN_ID_LABEL

if TYPE_CHECKING:
    from collections.abc import Callable

    from vs_sandbox.api import DockerCli

_DOCKER_TIMEOUT_SECONDS = 60.0


class OrphanReapError(RuntimeError):
    """An orphaned container could not be listed, reaped or removed."""


def reap_orphaned_agents(
    run_id: str,
    *,
    docker: DockerCli,
    log: Callable[[str], None],
    confinement_for: Callable[[str], agentshim.Confinement] | None = None,
) -> tuple[str, ...]:
    """End every agent process and container labelled with *run_id*; return their ids.

    ``confinement_for(container_id)`` is the confinement whose ``reap()`` kills the
    marked processes in that container; the default is ``docker exec`` through the
    host's ``docker`` client. Containers are handled in listing order.
    """
    confine = confinement_for or _exec_confinement
    orphans = _labelled_containers(docker, run_id)
    for container_id in orphans:
        log(f"[recover] ending orphaned agent container {container_id[:12]} of run {run_id}")
        try:
            confine(container_id).reap()
        except agentshim.ReapError as exc:
            msg = f"could not reap agents in orphaned container {container_id}: {exc}"
            raise OrphanReapError(msg) from exc
        _remove(docker, container_id)
    return orphans


def _exec_confinement(container_id: str) -> agentshim.Confinement:
    return agentshim.DockerExecConfinement(
        lambda: container_id, runner=agentshim.HostCommandExecutor(), env={}
    )


def _labelled_containers(docker: DockerCli, run_id: str) -> tuple[str, ...]:
    argv = ["docker", "ps", "-aq", "--filter", f"label={RUN_ID_LABEL}={run_id}"]
    result = _run(docker, argv, f"list the containers of run {run_id}")
    return tuple(line.strip() for line in result.stdout.splitlines() if line.strip())


def _remove(docker: DockerCli, container_id: str) -> None:
    _run(docker, ["docker", "rm", "-f", container_id], f"remove orphaned container {container_id}")


def _run(docker: DockerCli, argv: list[str], what: str) -> subprocess.CompletedProcess[str]:
    try:
        result = docker.run(argv, timeout_seconds=_DOCKER_TIMEOUT_SECONDS)
    except (OSError, subprocess.TimeoutExpired) as exc:
        msg = f"could not {what}: {exc}"
        raise OrphanReapError(msg) from exc
    if result.returncode != 0:
        msg = f"could not {what}: docker exited {result.returncode}: {result.stderr.strip()}"
        raise OrphanReapError(msg)
    return result
