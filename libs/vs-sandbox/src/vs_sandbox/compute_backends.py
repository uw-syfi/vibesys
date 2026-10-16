"""Compute backend protocol and registry for sandbox construction.

A ``ComputeBackendImpl`` knows how to:

1. Construct a command runner configured for its compute platform
   (image, GPU runtime args, env vars are all internal to the backend).
2. Optionally watch the platform for issues (CUDA: nvidia-smi contention).
3. Optionally migrate compute mid-run (CUDA: re-pick a less-loaded GPU).

Command runner classes (``DockerSandbox``, ``LocalShellRunner``)
stay backend-agnostic: they accept image/env/gpus as plain parameters.  The
compute backend supplies the right values for its platform inside
``make_sandbox``.
"""

from __future__ import annotations

from enum import StrEnum
from functools import cache
from importlib import import_module
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from vs_sandbox.accelerator_discovery import SystemAcceleratorDiscovery
from vs_sandbox.lifecycle import SandboxLifecycle
from vs_sandbox.local_shell import LocalShellRunner

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

    from vs_sandbox.docker_cli import DockerCli
    from vs_sandbox.execution import CommandRunner
    from vs_sandbox.host_resources import HostResource
    from vs_sandbox.lifecycle import SandboxLifecycleHooks
    from vs_sim.api import Clock, CommandProbe, PipedProcessLauncher, Threads


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
        lifecycle_hooks: list[SandboxLifecycleHooks] | None = None,
        attach_accelerator: bool = True,
        ephemeral: bool = False,
        container_image: str | None = None,
        auth_files: list[tuple[str, str]] | None = None,
        resources: Sequence[HostResource] = (),
        docker_in_docker: bool = False,
        same_path_workspace: bool = False,
        run_id: str | None = None,
    ) -> CommandRunner:
        """Construct (do not start) a sandbox configured for this backend.

        A Docker sandbox starts from a prebuilt agent image and installs
        nothing at start. A caller that needs root-only setup in a started
        Docker sandbox runs it itself with
        :func:`~vs_sandbox.lifecycle.execute_as_root`.

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

        ``run_id`` labels a Docker sandbox's container with the id of the run
        that started it, so a later reap can find a dead run's leftovers.
        Ignored by non-Docker sandboxes.
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

    def ensure_accelerator_available(self) -> None:
        """Raise when this host cannot provide the backend's accelerator right now.

        Called once per run, before any image build or sandbox is created, so a
        host that cannot honor ``make_sandbox(attach_accelerator=True)`` fails here,
        naming what is missing, instead of at the first container start. A backend
        whose accelerator has no host-side prerequisite to probe is a no-op.
        """
        ...


def make_local_shell_sandbox(
    *,
    host_workspace: str,
    env: dict[str, str],
    lifecycle_hooks: list[SandboxLifecycleHooks] | None = None,
    threads: Threads,
    launcher: PipedProcessLauncher,
) -> LocalShellRunner:
    """Construct the unconfined local-shell sandbox and run its lifecycle hooks.

    Every backend builds the local sandbox the same way, so the construction
    lives here once.
    """
    sandbox = LocalShellRunner(
        host_workspace, env=env, inherit_env=True, threads=threads, launcher=launcher
    )
    SandboxLifecycle(lifecycle_hooks).before_ready(sandbox)
    return sandbox


_REGISTRY: dict[ComputeBackend, Callable[..., ComputeBackendImpl]] = {}
_HOST_ONLY: set[ComputeBackend] = set()
_AVAILABILITY: dict[ComputeBackend, Callable[[CommandProbe], str | None]] = {}
_DEFAULT_IMAGES: dict[ComputeBackend, str] = {}
_PRESENCE: dict[ComputeBackend, Callable[[CommandProbe], bool]] = {}


def register_compute_backend(
    backend: ComputeBackend,
    factory: Callable[..., ComputeBackendImpl],
    *,
    host_only: bool = False,
    unavailable: Callable[[CommandProbe], str | None] | None = None,
    default_image: str | None = None,
) -> None:
    """Register the factory used to construct one compute backend.

    ``host_only`` declares that the backend's accelerator cannot be reached
    from a container, so its agent has to run on the host. ``default_image``
    is the image the factory falls back to when a run names none. ``unavailable``
    says, through a command probe, why this host cannot provide the backend's
    accelerator (``None`` when it can); it makes the decision the backend's own
    ``ensure_accelerator_available`` makes, and a backend without one always can.
    """
    _REGISTRY[backend] = factory
    if host_only:
        _HOST_ONLY.add(backend)
    else:
        _HOST_ONLY.discard(backend)
    if default_image is None:
        _DEFAULT_IMAGES.pop(backend, None)
    else:
        _DEFAULT_IMAGES[backend] = default_image
    if unavailable is None:
        _AVAILABILITY.pop(backend, None)
    else:
        _AVAILABILITY[backend] = unavailable


def register_backend_presence(
    backend: ComputeBackend, present: Callable[[CommandProbe], bool]
) -> None:
    """Declare how to tell, through a command probe, that this host has *backend*'s hardware.

    ``backend_provided`` reads it, besides the ``unavailable`` reason; a backend without
    one needs no hardware.
    """
    _PRESENCE[backend] = present


def accelerator_unavailable(backend: ComputeBackend, probe: CommandProbe) -> str | None:
    """Why this host cannot provide *backend*'s accelerator, asked through *probe*, or ``None``.

    A run raises this reason from ``ensure_accelerator_available`` before its image
    build; asking here lets a check made before the run report it instead.
    """
    _ensure_defaults()
    check = _AVAILABILITY.get(backend)
    return None if check is None else check(probe)


def backend_provided(backend: ComputeBackend, probe: CommandProbe) -> bool:
    """Whether this host has what *backend* targets, asked through *probe*.

    The one answer to "which backends can this host run": a backend that declares an
    unavailability reason (CUDA's) is provided when it has none, and one that declares
    how to detect its hardware (ROCm, Trainium, Metal) when that hardware is present.
    CPU, which needs no hardware, is always provided. Selection among a task's declared
    backends and the preflight's check of the chosen one both read it.
    """
    _ensure_defaults()
    if accelerator_unavailable(backend, probe) is not None:
        return False
    present = _PRESENCE.get(backend)
    return True if present is None else present(probe)


def backend_is_host_only(backend: ComputeBackend) -> bool:
    """Return whether *backend* declares that no container can reach its accelerator.

    Environment selection reads this capability instead of naming backends.
    """
    _ensure_defaults()
    return backend in _HOST_ONLY


def backend_default_image(backend: ComputeBackend) -> str | None:
    """Return the image *backend* runs in when the run names none, or ``None``.

    A task Dockerfile is built on top of it (as ``BASE_IMAGE``) before the run
    starts, before the backend itself is constructed. ``None`` means the
    backend declares no container image (Metal).
    """
    _ensure_defaults()
    return _DEFAULT_IMAGES.get(backend)


def create_compute_backend(  # noqa: PLR0913  # lint-waiver: LW-731406 [PLR0913]; one keyword per construction input every registered backend shares.
    # > A settings object would only rename these keywords for the one caller and the
    # > registered factories; splitting the factory per backend would branch on a
    # > backend name, which the registry exists to avoid.
    backend: ComputeBackend,
    log_dir: Path,
    *,
    log: Callable[[str], None] | None = None,
    image: str | None = None,
    docker: DockerCli,
    threads: Threads,
    launcher: PipedProcessLauncher,
    clock: Clock,
    probe: CommandProbe,
    events: object | None = None,
) -> ComputeBackendImpl:
    """Construct the registered implementation for one compute stack.

    *docker* runs the backend's ``docker`` commands, *threads* is the clock its
    sandboxes measure command timeouts on, *launcher* starts its host shell
    commands, and *probe* runs the status commands (``nvidia-smi``, ``rocm-smi``) it asks.
    ``clock`` counts seconds since the epoch; a backend stamps what it records with it.
    ``events`` is a backend-specific contention-events seam (only the CUDA
    backend reads it today); every registered factory accepts and may ignore
    it so this call stays uniform across backends.
    """
    _ensure_defaults()
    if backend not in _REGISTRY:
        message = f"No backend impl registered for {backend!r}"
        raise ValueError(message)
    return _REGISTRY[backend](
        log_dir=log_dir,
        log=log,
        image=image,
        docker=docker,
        threads=threads,
        launcher=launcher,
        clock=clock,
        probe=probe,
        events=events,
    )


# lint-waiver: LW-731481 [PLR0913]; the registry's shared construction keywords, one to one.
# > Grouping them would only rename the keywords create_compute_backend passes every
# > registered backend.
def _rocm_backend(  # noqa: PLR0913
    log_dir: Path,
    *,
    log: Callable[[str], None] | None,
    image: str | None,
    docker: DockerCli,
    threads: Threads,
    launcher: PipedProcessLauncher,
    clock: Clock,
    probe: CommandProbe,
    events: object | None = None,
) -> ComputeBackendImpl:
    """The ROCm backend, discovering its GPUs through ``probe``."""
    del events  # ROCm watches no contention; accepted so the registry call stays uniform
    rocm = import_module("vs_sandbox.rocm_backend")
    return rocm.RocmBackend(
        log_dir,
        log=log,
        image=image,
        docker=docker,
        threads=threads,
        launcher=launcher,
        clock=clock,
        accelerator_discovery=SystemAcceleratorDiscovery(probe=probe),
    )


# lint-waiver: LW-731482 [PLR0913]; the registry's shared construction keywords, one to one.
# > Grouping them would only rename the keywords create_compute_backend passes every
# > registered backend.
def _trainium_backend(  # noqa: PLR0913
    log_dir: Path,
    *,
    log: Callable[[str], None] | None,
    image: str | None,
    docker: DockerCli,
    threads: Threads,
    launcher: PipedProcessLauncher,
    clock: Clock,
    probe: CommandProbe,
    events: object | None = None,
) -> ComputeBackendImpl:
    """The Trainium backend, discovering its devices through ``probe``."""
    del events  # Trainium watches no contention; accepted so the registry call stays uniform
    trainium = import_module("vs_sandbox.trainium_backend")
    return trainium.TrainiumBackend(
        log_dir,
        log=log,
        image=image,
        docker=docker,
        threads=threads,
        launcher=launcher,
        clock=clock,
        accelerator_discovery=SystemAcceleratorDiscovery(probe=probe),
    )


_APPLE_CPU_BRAND = ("sysctl", "-n", "machdep.cpu.brand_string")
"""Names the CPU on macOS; Apple Silicon's brand starts with ``Apple`` (the helper's GPU query)."""


def _apple_silicon_present(probe: CommandProbe) -> bool:
    result = probe.run(_APPLE_CPU_BRAND, timeout_seconds=5.0)
    return result is not None and result.returncode == 0 and result.stdout.startswith("Apple ")


def _register_defaults() -> None:
    cuda = import_module("vs_sandbox.cuda_backend")
    local = import_module("vs_sandbox.local_compute_backend")
    rocm = import_module("vs_sandbox.rocm_backend")
    trainium = import_module("vs_sandbox.trainium_backend")

    register_compute_backend(
        ComputeBackend.CUDA,
        cuda.CudaBackend,
        default_image=cuda.DEFAULT_IMAGE,
        unavailable=cuda.cuda_accelerator_unavailable,
    )
    register_compute_backend(
        ComputeBackend.METAL,
        local.metal_backend,
        host_only=ComputeBackend.METAL in local.HOST_ONLY_BACKENDS,
    )
    register_compute_backend(
        ComputeBackend.TRAINIUM, _trainium_backend, default_image=trainium.DEFAULT_IMAGE
    )
    register_compute_backend(ComputeBackend.ROCM, _rocm_backend, default_image=rocm.DEFAULT_IMAGE)
    register_backend_presence(ComputeBackend.METAL, _apple_silicon_present)
    register_backend_presence(
        ComputeBackend.TRAINIUM,
        lambda probe: bool(
            SystemAcceleratorDiscovery(probe=probe).discover_trainium().device_nodes
        ),
    )
    register_backend_presence(
        ComputeBackend.ROCM,
        lambda probe: bool(SystemAcceleratorDiscovery(probe=probe).discover_rocm().device_nodes),
    )
    register_compute_backend(
        ComputeBackend.CPU, local.cpu_backend, default_image=local.DEFAULT_CPU_IMAGE
    )


@cache
def _ensure_defaults() -> None:
    _register_defaults()


__all__ = [
    "ComputeBackend",
    "ComputeBackendImpl",
    "ContentionMonitor",
    "Device",
    "SandboxKind",
    "accelerator_unavailable",
    "backend_default_image",
    "backend_is_host_only",
    "backend_provided",
    "create_compute_backend",
    "register_compute_backend",
]
