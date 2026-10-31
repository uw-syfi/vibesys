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
the run must not resume while an agent may still be running. Whether the removal
succeeded is read by ``vs_sandbox``'s one Docker lifecycle translator: a container
already gone or already being removed (one started with ``--rm`` that exited in
between) is released, not an error.
"""

from __future__ import annotations

import subprocess
from functools import partial
from typing import TYPE_CHECKING

import agentshim

from vs_sandbox.api import (
    INITIAL_CONTAINER_LIFE,
    RUN_ID_LABEL,
    STEP_TAG_ENV,
    RemovalReading,
    observe_removal,
    released,
    step_tag_env,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from vs_sandbox.api import DockerCli
from vs_sim.api import DISCARD

_DOCKER_TIMEOUT_SECONDS = 60.0

# Kills every process in the container whose initial environment holds the line
# ``$1=$2`` (the marker name and the step's tag) and prints how many it *found*, not how many
# it killed: a kill that fails (the process is dying, or not the sweep's to signal) must read
# as "still there", never as "nothing found", or the step would be called clear over a live
# writer. Like agentshim's sweep for its own marker, it never reads its own shell's or its
# helpers' environment as a match: the tag travels as arguments, not as environment. Nothing
# matched prints 0; a container without ``tr`` or ``grep`` cannot sweep and must say so (exit
# 127), not report clean.
#
# It runs as the container's own user, which is the agent's: the agent image's ``USER`` is the
# agent, and neither the agent's ``docker exec`` nor this one passes ``-u``. A process's
# environment is readable only by its owner (and by root with ``CAP_SYS_PTRACE``, which a
# default container lacks), so ``-u root`` here would read nothing of the agent's and report
# clean over a live writer. A process of another non-root user would be skipped the same way;
# the agent is never started as one.
_REAP_TAGGED_SCRIPT = (
    "command -v tr >/dev/null && command -v grep >/dev/null "
    "|| { echo 'reap needs tr and grep in the container' >&2; exit 127; }; "
    'self=$$; marker="$1=$2"; n=0; '
    "for d in /proc/[0-9]*; do "
    'pid=${d#/proc/}; [ "$pid" = "$self" ] && continue; '
    "tr '\\0' '\\n' 2>/dev/null < \"$d/environ\" | grep -qxF -- \"$marker\" "
    '&& { n=$((n+1)); kill -9 "$pid" 2>/dev/null; }; '
    'done; echo "$n"'
)

_MAX_SWEEPS = 8
"""Sweeps of one container before giving up: each signals everything it finds, so a sweep that
still finds processes after a few means they do not die (a stuck kernel) or are started again."""

# What the docker client says when the container is gone or stopped: none of its
# processes can still be running.
_NO_CONTAINER = ("no such container", "is not running", "no container with name or id")


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
    orphans = _labelled_containers(docker, run_id)
    reap_recorded_agents(orphans, docker=docker, log=log, confinement_for=confinement_for)
    return orphans


def reap_step_agents(
    run_id: str,
    tag: str,
    *,
    docker: DockerCli,
    log: Callable[[str], None],
    reap_tagged: Callable[[str, str], int] | None = None,
) -> tuple[str, ...]:
    """Kill every agent process carrying *tag* in the containers of *run_id*; return those searched.

    *tag* is the mark the step's processes were started with (``AgentTurnRequest.step_tag``).

    ``reap_tagged(container_id, tag)`` signals the tagged processes of one container and
    returns how many it found, or raises :class:`OrphanReapError` when it cannot search; the
    default is ``docker exec`` through *docker*. A container is swept again until a sweep finds
    none, and one that still has processes after a few sweeps raises: the step's work is then
    not proven gone. The containers are left running: only the processes of the step go.
    Idempotent, and a tag no process carries is not an error.
    """
    step_tag_env(tag)
    searched = _labelled_containers(docker, run_id)
    reap = reap_tagged or partial(_exec_reap_tagged, docker)
    for container_id in searched:
        log(f"[recover] ending agents of step {tag} in container {container_id[:12]}")
        _sweep_until_none(reap, container_id, tag)
    return searched


def reap_recorded_agents(
    containers: tuple[str, ...],
    *,
    docker: DockerCli,
    log: Callable[[str], None],
    confinement_for: Callable[[str], agentshim.Confinement] | None = None,
) -> None:
    """Reap exact immutable container IDs from a durable writer manifest.

    Removal acknowledgement is not termination proof. The environment must
    independently verify absence on the recorded daemon before reusing backing.
    """
    confine = confinement_for or _exec_confinement
    for container_id in containers:
        log(f"[recover] ending recorded agent container {container_id[:12]}")
        try:
            confine(container_id).reap()
        except agentshim.ReapError as exc:
            msg = f"could not reap agents in orphaned container {container_id}: {exc}"
            raise OrphanReapError(msg) from exc
        _remove(docker, container_id)


def _sweep_until_none(reap: Callable[[str, str], int], container_id: str, tag: str) -> None:
    for _ in range(_MAX_SWEEPS):
        if reap(container_id, tag) == 0:
            return
    msg = f"agents of step {tag} in container {container_id} are still running after {_MAX_SWEEPS} sweeps"
    raise OrphanReapError(msg)


def _exec_reap_tagged(docker: DockerCli, container_id: str, tag: str) -> int:
    what = f"end the agents of step {tag} in container {container_id}"
    argv = [
        "docker",
        "exec",
        container_id,
        "sh",
        "-c",
        _REAP_TAGGED_SCRIPT,
        "sh",
        STEP_TAG_ENV,
        tag,
    ]
    result = _run(docker, argv, what, check=False)
    if result.returncode == 0:
        try:
            return int(result.stdout.strip())
        except ValueError:
            msg = f"could not {what}: the sweep printed {result.stdout.strip()!r}, not a count"
            raise OrphanReapError(msg) from None
    if any(text in result.stderr.lower() for text in _NO_CONTAINER):
        return 0
    msg = f"could not {what}: docker exited {result.returncode}: {result.stderr.strip()}"
    raise OrphanReapError(msg)


def _exec_confinement(container_id: str) -> agentshim.Confinement:
    return agentshim.DockerExecConfinement(
        lambda: container_id, runner=agentshim.HostCommandExecutor(), env={}
    )


def _labelled_containers(docker: DockerCli, run_id: str) -> tuple[str, ...]:
    argv = ["docker", "ps", "-aq", "--filter", f"label={RUN_ID_LABEL}={run_id}"]
    result = _run(docker, argv, f"list the containers of run {run_id}")
    return tuple(line.strip() for line in result.stdout.splitlines() if line.strip())


def _remove(docker: DockerCli, container_id: str) -> None:
    what = f"remove orphaned container {container_id}"
    result = _run(docker, ["docker", "rm", "-f", container_id], what, check=False)
    life = observe_removal(INITIAL_CONTAINER_LIFE, RemovalReading(result.returncode, result.stderr))
    if not released(life):
        msg = f"could not {what}: docker exited {result.returncode}: {result.stderr.strip()}"
        raise OrphanReapError(msg)


def _run(
    docker: DockerCli, argv: list[str], what: str, *, check: bool = True
) -> subprocess.CompletedProcess[str]:
    """Run *argv*; a client that cannot finish, or (when *check*) a failure, is an error."""
    try:
        result = docker.run(argv, timeout_seconds=_DOCKER_TIMEOUT_SECONDS, output=DISCARD)
    except (OSError, subprocess.TimeoutExpired) as exc:
        msg = f"could not {what}: {exc}"
        raise OrphanReapError(msg) from exc
    if check and result.returncode != 0:
        msg = f"could not {what}: docker exited {result.returncode}: {result.stderr.strip()}"
        raise OrphanReapError(msg)
    return result
