"""Compute backend protocol and registry for sandbox construction.

A ``ComputeBackendImpl`` knows how to:

1. Construct a sandbox configured for its compute platform
   (image, GPU runtime args, env vars are all internal to the backend).
2. Optionally watch the platform for issues (CUDA: nvidia-smi contention).
3. Optionally migrate compute mid-run (CUDA: re-pick a less-loaded GPU).

Sandbox classes (``DockerSandbox``, ``LocalShellSandbox``)
stay backend-agnostic: they accept image/env/gpus as plain parameters.  The
compute backend supplies the right values for its platform inside
``make_sandbox``.
"""

from __future__ import annotations

from enum import StrEnum
from functools import cache
from importlib import import_module
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from vs_sandbox.lifecycle import SandboxLifecycle
from vs_sandbox.local_shell import LocalShellSandbox

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

    from vs_sandbox.execution import Sandbox
    from vs_sandbox.host_resources import HostResource
    from vs_sandbox.lifecycle import SandboxLifecycleHooks


class ComputeBackend(StrEnum):
    """Compute stacks supported by sandbox construction."""

    CUDA = "cuda"
    METAL = "metal"
    TRAINIUM = "trainium"
    ROCM = "rocm"
    CPU = "cpu"


class SandboxKind(StrEnum):
    """Where the agent's shell commands actually execute."""

    LOCAL = "local"
    DOCKER = "docker"


class Device(Protocol):
    """Minimum device interface run resources consume for logging and pinning."""

    index: int
    name: str


class ContentionMonitor(Protocol):
    """Background thread that reports platform contention (e.g. shared-GPU use)."""

    def start(self) -> None:
        """Begin reporting contention samples in the background."""
        ...

    def stop(self) -> None:
        """Stop monitoring and release background resources."""
        ...


@runtime_checkable
class ComputeBackendImpl(Protocol):
    """Per-platform backend.  See module docstring for the contract."""

    name: ComputeBackend

    def make_sandbox(  # noqa: PLR0913  # lint-waiver: LW-011111 [PLR0913]; Runtime-checkable ComputeBackendImpl exposes these sandbox controls as protocol keywords; a config object would break every backend implementation and caller.
        self,
        kind: SandboxKind,
        *,
        host_workspace: str,
        log_path: Path | str | None,
        bind_mounts: list[tuple[str, str, bool]],
        extra_env: dict[str, str],
        extra_init_commands: list[str] | None = None,
        lifecycle_hooks: list[SandboxLifecycleHooks] | None = None,
        attach_accelerator: bool = True,
        ephemeral: bool = False,
        container_image: str | None = None,
        auth_files: list[tuple[str, str]] | None = None,
        resources: Sequence[HostResource] = (),
    ) -> Sandbox:
        """Construct (do not start) a sandbox configured for this backend.

        ``extra_init_commands`` is ignored by every current backend: a
        Docker sandbox starts from a prebuilt agent image and installs
        nothing at start, and a local sandbox has no separate install step.
        Kept for protocol parity with a future backend that needs it.

        ``lifecycle_hooks`` are invoked before the sandbox becomes ready,
        during both initial creation and replacement.

        ``attach_accelerator=False`` creates a CPU-only control-plane sandbox
        while preserving the target backend's image and tooling. Remote
        dispatch environments use this for local editor containers.

        ``ephemeral=True`` excludes a short-lived framework setup sandbox from
        backend restart or device-reselection tracking.

        ``container_image`` pins a Docker sandbox to a resolved image ID. It is
        ignored by non-Docker sandboxes.

        ``auth_files`` names ``(staged source, agent-home destination)`` pairs
        a Docker sandbox copies in as root at start, then chowns to the agent
        user. Ignored by non-Docker sandboxes.

        ``resources`` is the caller's :class:`~vs_sandbox.host_resources.HostResource`
        list for this sandbox, forwarded to ``DockerSandbox(resources=...)``
        unchanged: a Docker sandbox lowers it to bind mounts and consults it
        for :meth:`agent_path`. ``bind_mounts`` keeps working for a caller that
        still builds its own mount tuples directly; the two combine rather
        than one replacing the other. Ignored by non-Docker sandboxes, whose
        accelerator device and model-volume mounts stay backend-specific.
        """
        ...

    def make_monitor(self, log_dir: Path) -> ContentionMonitor | None:
        """Create a contention monitor when the backend supports one."""
        ...

    def reselect_device(self) -> None:
        """Re-pick the optimal device for this backend and restart sandboxes.

        For example, migrate from a less-loaded GPU and restart affected
        sandboxes in place.

        Each restarted sandbox re-runs its lifecycle hooks automatically as
        part of ``start()``.  No-op for backends without rebalancing.
        """
        ...


def make_local_shell_sandbox(
    *,
    host_workspace: str,
    env: dict[str, str],
    lifecycle_hooks: list[SandboxLifecycleHooks] | None = None,
) -> LocalShellSandbox:
    """Construct the unconfined local-shell sandbox and run its lifecycle hooks.

    Every backend builds the local sandbox the same way, so the construction
    lives here once.
    """
    sandbox = LocalShellSandbox(host_workspace, env=env, inherit_env=True)
    SandboxLifecycle(lifecycle_hooks).before_ready(sandbox)
    return sandbox


_REGISTRY: dict[ComputeBackend, Callable[..., ComputeBackendImpl]] = {}


def register_compute_backend(
    backend: ComputeBackend,
    factory: Callable[..., ComputeBackendImpl],
) -> None:
    """Register the factory used to construct one compute backend."""
    _REGISTRY[backend] = factory


def create_compute_backend(
    backend: ComputeBackend,
    log_dir: Path,
    *,
    log: Callable[[str], None] | None = None,
    image: str | None = None,
) -> ComputeBackendImpl:
    """Construct the registered implementation for one compute stack."""
    _ensure_defaults()
    if backend not in _REGISTRY:
        message = f"No backend impl registered for {backend!r}"
        raise ValueError(message)
    return _REGISTRY[backend](log_dir=log_dir, log=log, image=image)


def _register_defaults() -> None:
    cuda = import_module("vs_sandbox.cuda_backend")
    local = import_module("vs_sandbox.local_compute_backend")
    rocm = import_module("vs_sandbox.rocm_backend")
    trainium = import_module("vs_sandbox.trainium_backend")

    register_compute_backend(ComputeBackend.CUDA, cuda.CudaBackend)
    register_compute_backend(ComputeBackend.METAL, local.metal_backend)
    register_compute_backend(ComputeBackend.TRAINIUM, trainium.TrainiumBackend)
    register_compute_backend(ComputeBackend.ROCM, rocm.RocmBackend)
    register_compute_backend(ComputeBackend.CPU, local.cpu_backend)


@cache
def _ensure_defaults() -> None:
    _register_defaults()


__all__ = [
    "ComputeBackend",
    "ComputeBackendImpl",
    "ContentionMonitor",
    "Device",
    "SandboxKind",
    "create_compute_backend",
    "register_compute_backend",
]
