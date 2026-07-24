"""The entire Omnigent contact surface for the Docker path — one module.

``DockerSandboxLauncher`` is the only class in this spike that imports
``omnigent``. It subclasses Omnigent's ``SandboxLauncher`` ABC and maps each
method onto a VibeSys-owned :class:`~container.ContainerSandbox`, adding no
container logic of its own. This is the "isolate hard" boundary: churn in the
alpha ABC touches this file alone, and a decision to walk away deletes this file
and leaves ``container.py`` / ``spec.py`` (the code VibeSys owns regardless)
intact.

Injection follows Omnigent's own documented embedding recipe
(``omnigent/server/managed_hosts.py`` module docstring)::

    ManagedSandboxConfig(
        server_url="http://host.docker.internal:6767",
        launcher_factory=lambda: DockerSandboxLauncher(sandbox, spec),
        token_ttl_s=90000,
    )

No registry patch and no fork of Omnigent are required.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

from container import ContainerSandbox
from omnigent.onboarding.sandboxes.base import (
    RemoteCommandResult,
    RemoteProcess,
    SandboxLauncher,
)
from spec import VibesysSandboxSpec


class _RemoteProcessAdapter(RemoteProcess):
    """Wrap a VibeSys ``ContainerProcess`` as an Omnigent ``RemoteProcess``."""

    def __init__(self, inner: object) -> None:
        self._inner = inner

    def lines(self) -> Iterator[str]:
        return self._inner.lines()  # type: ignore[attr-defined]

    def wait(self) -> int:
        return self._inner.wait()  # type: ignore[attr-defined]

    def close(self) -> None:
        self._inner.close()  # type: ignore[attr-defined]


class DockerSandboxLauncher(SandboxLauncher):
    """Run an ``omnigent host`` inside a VibeSys-managed local Docker container.

    Only three methods are abstract on the ABC (``prepare``, ``provision``,
    ``run``); the rest are overridden where the container model differs from
    Omnigent's default (notably ``materialize_workspace``, which bind-mounts
    instead of ``git clone``).
    """

    provider = "vibesys-docker"
    supports_cli_bootstrap = False
    supports_local_port_forward = False
    can_resume = False

    def __init__(
        self,
        sandbox: ContainerSandbox,
        spec: VibesysSandboxSpec,
        *,
        name: str = "vibesys-agent",
    ) -> None:
        self._sandbox = sandbox
        self._spec = spec
        self._name = name

    # -- abstract methods -------------------------------------------------

    def prepare(self) -> None:
        # Local preflight hook (idempotent, per the ABC). A production adapter
        # verifies the Docker daemon is reachable and the image is present or
        # pullable here; the container work itself is deferred to ``provision``.
        return None

    def provision(self, name: str) -> str:
        return self._sandbox.create(self._spec, name=self._name)

    def run(self, sandbox_id: str, command: str, *, check: bool = True) -> RemoteCommandResult:
        result = self._sandbox.exec(sandbox_id, command, check=check)
        return RemoteCommandResult(
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
        )

    # -- overridden defaults ---------------------------------------------

    def run_background(
        self,
        sandbox_id: str,
        command: str,
        *,
        log_path: str = "/tmp/omnigent-host.log",
    ) -> RemoteCommandResult:
        self._sandbox.exec_background(sandbox_id, f"{command} >{log_path} 2>&1")
        return RemoteCommandResult(returncode=0, stdout="", stderr="")

    def put(self, sandbox_id: str, local_path: Path, remote_path: str) -> None:
        self._sandbox.copy_in(sandbox_id, local_path, remote_path)

    def stream_exec(self, sandbox_id: str, command: str, *, pty: bool = False) -> RemoteProcess:
        del pty  # the local daemon path does not need a PTY
        return _RemoteProcessAdapter(self._sandbox.stream(sandbox_id, command))

    def materialize_workspace(
        self,
        sandbox_id: str,
        *,
        workspace: str,
        repo_url: str,
        repo_branch: str | None,
        repo_name: str | None,
        on_stage: Callable[[str], None] | None = None,
    ) -> str:
        """Resolve to the bind-mounted workspace instead of cloning.

        This is the override the issue anticipated: Omnigent's default clones
        ``repo_url`` into the box, but the VibeSys workspace is already present
        at ``spec.workdir`` via the bind mount, so materialization is a no-op
        that just returns that path. The live host workspace — not a fresh
        clone — is what the agent edits, preserving the ``AgentRunner``
        contract.
        """
        del workspace, repo_url, repo_branch, repo_name
        if on_stage is not None:
            on_stage("starting")
        return self._spec.workdir

    def is_running(self, sandbox_id: str) -> bool | None:
        return self._sandbox.is_running(sandbox_id)

    def terminate(self, sandbox_id: str) -> None:
        self._sandbox.remove(sandbox_id)
