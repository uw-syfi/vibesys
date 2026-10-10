"""A ``DockerSandbox`` double with the surface the agent driver uses."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


@dataclass
class FakeDockerConfinement:
    """A ``WorkspaceSandbox`` double shaped like ``vs_sandbox.DockerSandbox``.

    ``wrap`` accepts the optional ``cwd`` the real sandbox does (the
    capability :func:`confine_to_sandbox` probes for), maps it through the
    same workspace-prefix rule ``agent_path`` uses, and renders its own extra
    environment as ``-e`` flags exactly the way the real sandbox's ``wrap``
    does.
    """

    workspace: Path
    container_id: str = "container-1"
    extra_env: dict[str, str] = field(default_factory=dict)
    home: str = "/home/agent"
    container_path: str = "/usr/local/bin:/usr/bin"

    def agent_path(self, path: Path | str) -> str:
        """The path as the container sees it: the workspace maps to ``/workspace``."""
        normalized = str(path)
        workspace = str(self.workspace)
        if normalized == workspace:
            return "/workspace"
        if normalized.startswith(workspace + "/"):
            return "/workspace" + normalized[len(workspace) :]
        return normalized

    def wrap(self, argv: list[str], cwd: Path | str | None = None) -> list[str]:
        """The ``docker exec`` argv that runs ``argv`` in the container."""
        workdir = self.agent_path(cwd) if cwd is not None else "/workspace"
        env_flags = [
            flag for key, value in self.extra_env.items() for flag in ("-e", f"{key}={value}")
        ]
        return ["docker", "exec", "-i", "-w", workdir, *env_flags, self.container_id, *argv]

    @property
    def env(self) -> dict[str, str]:
        """The environment a process in the container sees."""
        return {"HOME": self.home, "PATH": self.container_path, **self.extra_env}
