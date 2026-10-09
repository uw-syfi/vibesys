"""Docker-in-Docker for tasks whose candidate is itself a container topology.

A microservice candidate is a set of containers the agent must build, start,
and trace. A task that declares ``[environment] docker_in_docker = true`` gets a
Docker sandbox that runs under the Sysbox runtime (no ``--privileged``, no host
socket) with a Docker daemon of its own started inside it. The daemon's state
lives and dies with the sandbox container. There is no other way to give a
sandbox a container runtime: the host's Docker socket is never mounted.

A daemon resolves bind-mount sources in its own namespace, so the workspace is
mounted at the same path inside the container as on the host
(:func:`workspace_container_root`): a compose file's relative or absolute bind
source then names the same directory for the agent and for the daemon.

This module is the single owner of those facts.
:class:`~vs_sandbox.docker_sandbox.DockerSandbox` applies them.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from vs_sandbox.docker_cli import DockerCli

#: The ``docker run --runtime`` name Sysbox registers with the host daemon.
SYSBOX_DOCKER_RUNTIME = "sysbox-runc"

#: The workspace's mount point when no container runtime is requested.
DEFAULT_CONTAINER_ROOT = "/workspace"

#: Seconds the nested daemon gets to answer ``docker info`` after it starts.
NESTED_DAEMON_READY_TIMEOUT_S = 60
_READY_OUTER_SLACK_S = 30
_RUNTIME_PROBE_TIMEOUT_S = 30

NESTED_DAEMON_LOG = "/var/log/vibesys-dockerd.log"

# Run as root inside the sandbox container, detached. ``exec`` makes dockerd
# the process the detached exec tracks. The group is the image's ``docker``
# group, which the agent user belongs to, so the agent can use the socket
# without being root.
NESTED_DAEMON_START_SCRIPT = (
    f"exec dockerd --host=unix:///var/run/docker.sock --group docker >{NESTED_DAEMON_LOG} 2>&1"
)

# Passed as ``sh -c SCRIPT sh SECONDS``. Polls inside the container so the
# host side issues one bounded command and a fake engine can recognise the
# readiness check by this exact script. On failure the daemon's own log tail is
# the diagnostic, because a daemon that dies at startup says why only there.
NESTED_DAEMON_READY_SCRIPT = (
    'i=0; while [ "$i" -lt "$1" ]; do'
    " if docker info >/dev/null 2>&1; then exit 0; fi;"
    " i=$((i+1)); sleep 1; done;"
    ' echo "nested Docker daemon not ready after $1s" >&2;'
    f" tail -n 40 {NESTED_DAEMON_LOG} >&2 2>/dev/null; exit 1"
)


def workspace_container_root(host_workspace: str, *, same_path: bool) -> str:
    """Return where a Docker sandbox mounts the workspace.

    The host path itself when *same_path* is set, and ``/workspace`` otherwise.
    Two kinds of sandbox need the same path. A docker-in-docker sandbox needs
    it so the agent and the nested daemon agree on every bind source. A
    sandbox whose agent talks to a host-owned broker over a shared filesystem
    (Slurm) needs it so a working directory the agent sends is valid on the
    host. Every container path derived from the workspace goes through this
    one answer.
    """
    return str(Path(host_workspace)) if same_path else DEFAULT_CONTAINER_ROOT


class ContainerRuntimeUnavailableError(RuntimeError):
    """The Sysbox runtime a docker-in-docker task needs is not available.

    Raised before any container is created, naming the missing piece. Nothing
    falls back to the host socket or to a plain container.
    """

    @classmethod
    def sysbox_missing(cls, available: list[str]) -> ContainerRuntimeUnavailableError:
        """Describe a Docker daemon that does not register the Sysbox runtime."""
        listed = ", ".join(sorted(available)) or "none"
        return cls(
            f"The task declares [environment] docker_in_docker = true, which needs the "
            f"'{SYSBOX_DOCKER_RUNTIME}' Docker runtime, but the Docker daemon on this host "
            f"does not register it (available runtimes: {listed}). Install Sysbox "
            f"(https://github.com/nestybox/sysbox) or run this task on a host that has it. "
            f"The host Docker socket is never used as a substitute."
        )

    @classmethod
    def runtime_unreadable(cls, detail: str) -> ContainerRuntimeUnavailableError:
        """Describe a Docker daemon whose runtimes could not be listed."""
        return cls(
            f"Could not list the Docker runtimes needed to check for "
            f"'{SYSBOX_DOCKER_RUNTIME}': {detail}"
        )

    @classmethod
    def runtime_listing_unparseable(cls, output: str) -> ContainerRuntimeUnavailableError:
        """Describe ``docker info`` output that is not a JSON object of runtimes."""
        return cls.runtime_unreadable(f"expected a JSON object of runtimes, got {output[:200]!r}")


class NestedDaemonError(RuntimeError):
    """The Docker daemon inside a sandbox container did not become ready."""


def require_sysbox_runtime(docker: DockerCli) -> None:
    """Raise :class:`ContainerRuntimeUnavailableError` unless Sysbox is registered.

    Pure observation: nothing is created, so a caller can run this before any
    expensive image build.
    """
    try:
        result = docker.run(
            ["docker", "info", "--format", "{{json .Runtimes}}"],
            timeout_seconds=_RUNTIME_PROBE_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ContainerRuntimeUnavailableError.runtime_unreadable(str(exc)) from exc
    if result.returncode != 0:
        raise ContainerRuntimeUnavailableError.runtime_unreadable(
            result.stderr.strip() or f"exit {result.returncode}"
        )
    try:
        runtimes = json.loads(result.stdout.strip() or "null")
    except json.JSONDecodeError as exc:
        raise ContainerRuntimeUnavailableError.runtime_listing_unparseable(
            result.stdout.strip()
        ) from exc
    if not isinstance(runtimes, dict):
        raise ContainerRuntimeUnavailableError.runtime_listing_unparseable(result.stdout.strip())
    if SYSBOX_DOCKER_RUNTIME not in runtimes:
        raise ContainerRuntimeUnavailableError.sysbox_missing([str(name) for name in runtimes])


def start_nested_daemon(
    docker: DockerCli,
    container_id: str,
    *,
    ready_timeout_seconds: int = NESTED_DAEMON_READY_TIMEOUT_S,
) -> None:
    """Start a Docker daemon inside *container_id* and wait until it answers.

    Both steps run as root through ``docker exec``: a detached start, then one
    bounded readiness check. Raises :class:`NestedDaemonError` with the
    daemon's log tail when it never becomes ready.
    """
    start = [
        "docker",
        "exec",
        "-u",
        "root",
        "-d",
        container_id,
        "sh",
        "-c",
        NESTED_DAEMON_START_SCRIPT,
    ]
    started = docker.run(start, timeout_seconds=_RUNTIME_PROBE_TIMEOUT_S)
    if started.returncode != 0:
        message = (
            f"could not start the nested Docker daemon (exit {started.returncode}): "
            f"{started.stderr.strip()[:500]}"
        )
        raise NestedDaemonError(message)
    ready = [
        "docker", "exec", "-u", "root", container_id, "sh", "-c",
        NESTED_DAEMON_READY_SCRIPT, "sh", str(ready_timeout_seconds),
    ]  # fmt: skip
    try:
        result = docker.run(ready, timeout_seconds=ready_timeout_seconds + _READY_OUTER_SLACK_S)
    except subprocess.TimeoutExpired as exc:
        message = f"nested Docker daemon readiness check timed out after {exc.timeout}s"
        raise NestedDaemonError(message) from exc
    if result.returncode != 0:
        message = f"nested Docker daemon is not ready:\n{result.stderr.strip()[:2000]}"
        raise NestedDaemonError(message)


__all__ = [
    "DEFAULT_CONTAINER_ROOT",
    "NESTED_DAEMON_READY_SCRIPT",
    "NESTED_DAEMON_READY_TIMEOUT_S",
    "NESTED_DAEMON_START_SCRIPT",
    "SYSBOX_DOCKER_RUNTIME",
    "ContainerRuntimeUnavailableError",
    "NestedDaemonError",
    "require_sysbox_runtime",
    "start_nested_daemon",
    "workspace_container_root",
]
