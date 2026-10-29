"""How a brokered job is confined on the node that runs it.

An agent's own sandbox is wherever the agent runs, and that is now a Docker
container on the submit host. A job the agent asks for runs elsewhere: on a
compute node, where Docker is normally unavailable. So its confinement is a
separate policy, chosen explicitly by whoever composes the broker, and applied
with the host sandbox mechanisms (bubblewrap, or Seatbelt on macOS) to the
job's argv. Nothing here knows about the agent's container.
"""

from __future__ import annotations

from functools import cache
from typing import TYPE_CHECKING, Protocol

from vs_sandbox.host_sandbox import build_remote_job_prefix

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

    from vs_sandbox.host_resources import HostResource
    from vs_sandbox.project_paths import ProjectPathPolicy


class JobConfinement(Protocol):
    """Wraps a job's argv so it runs confined to one workspace."""

    def wrap(self, workspace: Path, argv: Sequence[str]) -> list[str]:
        """Return *argv* prefixed with the confinement for *workspace*.

        Raises:
            vs_sandbox.api.SandboxUnavailableError: When the confinement
                cannot be enforced. Nothing falls back to an unconfined job.
        """
        ...


class HostJobConfinement:
    """Confine a job with the host sandbox (bubblewrap, or Seatbelt on macOS).

    The job sees *resources* (the host toolchains it needs) read-only and the
    workspace it was asked to run in, with the project path policy applied
    exactly as for an agent on the host. Enforcement is required, so a host
    without the mechanism refuses the job.

    The job runs on another machine, so the host-sandbox builder emits only a
    prefix that the compute node can resolve. Bubblewrap and Seatbelt are named
    by program name and found through the job's own ``PATH``. Landlock is
    refused because its Python entry point is local to the submit host.
    """

    def __init__(
        self,
        *,
        env: Mapping[str, str],
        resources: Sequence[HostResource],
        project_path_policy: ProjectPathPolicy,
    ) -> None:
        """Bind the host environment, resources, and path policy the job is confined by."""
        self._env = dict(env)
        self._resources = tuple(resources)
        self._project_path_policy = project_path_policy
        self._prefixes = cache(self._prefix)

    def wrap(self, workspace: Path, argv: Sequence[str]) -> list[str]:
        """Return *argv* run inside the host confinement for *workspace*."""
        return [*self._prefixes(workspace), *argv]

    def _prefix(self, workspace: Path) -> list[str]:
        return build_remote_job_prefix(
            workspace,
            env=self._env,
            resources=self._resources,
            project_path_policy=self._project_path_policy,
        )
