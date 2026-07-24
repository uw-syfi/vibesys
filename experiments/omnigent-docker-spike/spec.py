"""VibeSys-owned container resource spec — the isolation boundary.

This module carries zero Omnigent dependency. It is the VibeSys-side vocabulary
for "what container should run this agent": image, GPU/device passthrough, bind
mounts, and environment. The Omnigent seam (``omnigent_launcher.py``) consumes a
:class:`VibesysSandboxSpec` but never defines one, so the resource policy that
Omnigent's ``provision(name: str) -> str`` contract cannot express lives here,
owned by VibeSys and unaffected by churn in Omnigent's internal ABC.

The fields mirror what ``libs/vs-sandbox``'s Docker path already threads today
(image selection, ``--gpus``, ``--device`` binds, workspace mount); this spike
does not re-derive them, it names the subset the Omnigent boundary would need.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class BindMount:
    """A host path exposed inside the container."""

    source: Path
    target: str
    read_only: bool = False

    def to_docker_arg(self) -> str:
        suffix = ":ro" if self.read_only else ""
        return f"{self.source}:{self.target}{suffix}"


@dataclass(frozen=True)
class VibesysSandboxSpec:
    """Everything VibeSys must be able to say about an agent container.

    ``provision(name)`` on Omnigent's ``SandboxLauncher`` takes only a name, so
    every field below has to live in launcher-instance state. That is exactly
    the pattern Omnigent's own Modal launcher (``image=``) and Kubernetes
    launcher (``resources=``) use; this spec is the VibeSys equivalent.
    """

    image: str
    workspace: BindMount
    """The live host workspace, bind-mounted (not git-cloned) into the box."""

    workdir: str = "/workspace"
    gpus: str | None = None
    """Value for ``docker run --gpus`` (e.g. ``"device=0,1"`` or ``"all"``)."""

    devices: Sequence[str] = ()
    """Raw ``--device`` binds (e.g. ``/dev/nvidia0``, ``/dev/neuron0``)."""

    extra_mounts: Sequence[BindMount] = ()
    shm_size: str | None = None
    env: Mapping[str, str] = field(default_factory=dict)

    def docker_run_argv(self, *, name: str, sleep_entrypoint: bool = True) -> list[str]:
        """Build the ``docker run`` argument vector this spec implies.

        Kept pure and side-effect-free so a GPU/device spec can be asserted in a
        test on a host that has no GPU — the argv is the contract, the daemon
        call is the integration.
        """
        argv = ["docker", "run", "-d", "--name", name, "-w", self.workdir]
        if self.gpus is not None:
            argv += ["--gpus", self.gpus]
        for dev in self.devices:
            argv += ["--device", dev]
        if self.shm_size is not None:
            argv += ["--shm-size", self.shm_size]
        argv += ["-v", self.workspace.to_docker_arg()]
        for mount in self.extra_mounts:
            argv += ["-v", mount.to_docker_arg()]
        for key, value in self.env.items():
            argv += ["-e", f"{key}={value}"]
        argv.append(self.image)
        if sleep_entrypoint:
            # Hold the container open so the launcher can exec into it, matching
            # Omnigent's exec-model providers (Modal/Daytona) whose box is a bare
            # long-lived host the server execs against.
            argv += ["sleep", "infinity"]
        return argv
