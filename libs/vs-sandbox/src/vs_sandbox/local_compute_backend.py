"""No-device compute backend, shared by Metal and CPU.

Some targets run on the host with no accelerator the sandbox layer can reach:

- ``METAL`` — Apple Silicon: macOS Docker has no Metal/MPS passthrough and
  Modal offers no Apple GPUs.
- ``CPU`` — no GPU at all (CPU-bound targets: KV stores, networking servers).

Both have no device to select, no contention monitor, and no device migration.
Metal remains local-only because Docker/Modal cannot expose MPS. CPU can also
run inside Docker because it needs no accelerator passthrough. Per-platform
*prompt* guidance (MPS vs pure-CPU) lives in the backend fragments under
``prompts/backend/<name>/`` — not here.
"""

from __future__ import annotations

from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING

from vs_sandbox.compute_backends import (
    ComputeBackend,
    ContentionMonitor,
    SandboxKind,
    make_local_shell_sandbox,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from vs_sandbox.docker_cli import DockerCli
    from vs_sandbox.execution import CommandRunner
    from vs_sandbox.host_resources import HostResource
    from vs_sandbox.lifecycle import SandboxLifecycleHooks
    from vs_sim.api import Clock, CommandProbe, PipedProcessLauncher, Threads

_DEFAULT_CPU_IMAGE = "python:3.12-bookworm"

HOST_ONLY_BACKENDS = frozenset({ComputeBackend.METAL})
"""Backends whose accelerator no container can reach (the capability's one source)."""


class LocalBackend:
    """No-device backend (Metal / CPU) — hardware hooks are no-ops."""

    # lint-waiver: LW-261405 [PLR0913]; the platform identity plus the registry's shared keywords.
    # > Grouping them would only rename the keywords create_compute_backend passes every
    # > registered backend, and the Docker CLI and clock are independent effects.
    def __init__(  # noqa: PLR0913
        self,
        name: ComputeBackend,
        log_dir: Path,
        *,
        log: Callable[[str], None] | None = None,
        image: str | None = None,
        docker: DockerCli,
        threads: Threads,
        launcher: PipedProcessLauncher,
    ) -> None:
        """Configure the Metal or CPU backend from its platform identity."""
        self.name = name
        if name in HOST_ONLY_BACKENDS:
            self._unavailable_reason = (
                "Docker on macOS can't access Metal/MPS, and Modal does not offer Apple GPUs"
            )
            self._supports_docker = False
        else:
            self._unavailable_reason = "Modal CPU execution is not wired up for this backend"
            self._supports_docker = True
        self.log_dir = Path(log_dir)
        self._lprint = log or print
        self.image = image
        self._docker = docker
        self._threads = threads
        self._launcher = launcher
        # No accelerator to pick — kept for protocol parity with other backends
        # (e.g. run resources read ``selected_device``).
        self.selected_device = None

    # -- ComputeBackendImpl protocol -----------------------------------------

    def make_sandbox(  # noqa: PLR0913  # lint-waiver: LW-011112 [PLR0913]; HostEnvironment calls this structural ComputeBackendImpl method with the shared named sandbox options; changing it would break backend dispatch parity.
        self,
        kind: SandboxKind,
        *,
        host_workspace: str,
        log_path: Path | str | None,
        bind_mounts: list[tuple[str, str, bool]] | None = None,
        extra_env: dict[str, str] | None = None,
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
        """Create a local or supported Docker sandbox for this backend."""
        # Deferred: importing DockerSandbox registers process-wide signal and
        # atexit handlers. Registration must stay side-effect free.
        docker_sandbox = import_module("vs_sandbox.api").DockerSandbox

        del attach_accelerator, ephemeral
        bind_mounts = list(bind_mounts or [])
        extra_env = dict(extra_env or {})
        lifecycle_hooks = lifecycle_hooks or []

        if kind is SandboxKind.LOCAL:
            return make_local_shell_sandbox(
                host_workspace=host_workspace,
                env=extra_env,
                lifecycle_hooks=lifecycle_hooks,
                threads=self._threads,
                launcher=self._launcher,
            )
        if kind is SandboxKind.DOCKER and self._supports_docker:
            if self.image is None:
                _exception_message_2 = f"{self.name.value} backend requires a Docker image"
                raise ValueError(_exception_message_2)
            return docker_sandbox(
                host_workspace=host_workspace,
                # ``DockerEnvironment.open()`` (the plain Docker path)
                # always resolves and passes an agent image, so this only
                # falls back to the backend's own base image for a caller
                # that builds its own Docker sandbox without one — Modal and
                # SkyPilot's CPU-only local editor container, which still
                # installs everything per-run until their own image work
                # (#676, #679) lands.
                image=container_image or self.image,
                gpus=None,
                bind_mounts=bind_mounts,
                resources=resources,
                env=extra_env,
                log_path=log_path,
                auth_files=auth_files,
                lifecycle_hooks=lifecycle_hooks,
                docker=self._docker,
                threads=self._threads,
                docker_in_docker=docker_in_docker,
                same_path_workspace=same_path_workspace,
                run_id=run_id,
            )
        if kind is SandboxKind.DOCKER:
            _exception_message = f"{self.name.value} backend only supports local execution; SandboxKind.{kind.name} is unavailable ({self._unavailable_reason})."
            raise ValueError(_exception_message)
        message = f"Unknown sandbox kind: {kind!r}"
        raise ValueError(message)

    def ensure_accelerator_available(self) -> None:
        """No host prerequisite to probe: Metal has no passthrough, CPU needs no device."""
        return

    def make_monitor(self, log_dir: Path) -> ContentionMonitor | None:
        """Return no monitor because host-only backends have no device contention."""
        del log_dir
        return None

    def reselect_device(self) -> None:
        """Do nothing because the host backend does not select an accelerator."""
        return


# Platform-bound constructors — one per local-only backend, registered in
# :mod:`backends` just like the dedicated CUDA/Trainium impl classes. They
# pin the two things a local backend varies: its identity and the message
# shown when Docker/Modal (which can't reach the accelerator) is requested.
# Signatures mirror ``backends.get``'s call convention.


# lint-waiver: LW-261406 [PLR0913]; the registry's shared construction keywords, one to one.
# > Grouping them would only rename the keywords create_compute_backend passes every
# > registered backend.
def metal_backend(  # noqa: PLR0913
    log_dir: Path,
    *,
    log: Callable[[str], None] | None = None,
    image: str | None = None,
    docker: DockerCli,
    threads: Threads,
    launcher: PipedProcessLauncher,
    clock: Clock,
    probe: CommandProbe,
) -> LocalBackend:
    """Apple Silicon / Metal backend (local execution only); it records no timestamps."""
    del clock, probe
    return LocalBackend(
        ComputeBackend.METAL,
        log_dir,
        log=log,
        image=image,
        docker=docker,
        threads=threads,
        launcher=launcher,
    )


# lint-waiver: LW-261407 [PLR0913]; the registry's shared construction keywords, one to one.
# > Grouping them would only rename the keywords create_compute_backend passes every
# > registered backend.
def cpu_backend(  # noqa: PLR0913
    log_dir: Path,
    *,
    log: Callable[[str], None] | None = None,
    image: str | None = None,
    docker: DockerCli,
    threads: Threads,
    launcher: PipedProcessLauncher,
    clock: Clock,
    probe: CommandProbe,
) -> LocalBackend:
    """CPU-only backend (no GPU; CPU-bound targets like KV stores / servers); no timestamps."""
    del clock, probe
    return LocalBackend(
        ComputeBackend.CPU,
        log_dir,
        log=log,
        image=image or _DEFAULT_CPU_IMAGE,
        docker=docker,
        threads=threads,
        launcher=launcher,
    )
