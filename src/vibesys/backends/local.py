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

from collections.abc import Callable, Sequence  # noqa: TC003  # tracked: #288
from pathlib import Path
from typing import TYPE_CHECKING

from vibesys.backends.base import (
    ContentionMonitor,
    ModalOptions,
    SandboxKind,
    make_local_shell_sandbox,
)
from vibesys.constants import ComputeBackend
from vibesys.profilers import ProfilerKind

if TYPE_CHECKING:
    # Annotation only; deepagents pulls langchain + anthropic (~seconds).
    from deepagents.backends.protocol import SandboxBackendProtocol

    from vs_sandbox.host_resources import HostResource
    from vs_sandbox.lifecycle import SandboxLifecycleHooks

_DEFAULT_CPU_IMAGE = "python:3.12-bookworm"


class LocalBackend:
    """No-device backend (Metal / CPU) — hardware hooks are no-ops."""

    def __init__(  # noqa: D107, PLR0913  # tracked: #288
        self,
        name: ComputeBackend,
        log_dir: Path,
        *,
        log: Callable[[str], None] | None = None,
        image: str | None = None,
        unavailable_reason: str,
        profiler_kind: ProfilerKind = ProfilerKind.TORCH,
        supports_docker: bool = False,
    ) -> None:
        self.name = name
        self.profiler_kind = profiler_kind
        self.log_dir = Path(log_dir)
        self._lprint = log or print
        self.image = image
        self._unavailable_reason = unavailable_reason
        self._supports_docker = supports_docker
        # No accelerator to pick — kept for protocol parity with other backends
        # (e.g. _RunContext reads ``selected_device``).
        self.selected_device = None

    # -- ComputeBackendImpl protocol -----------------------------------------

    def make_sandbox(  # noqa: D102, PLR0913  # tracked: #288
        self,
        kind: SandboxKind,
        *,
        host_workspace: str,
        log_path: Path | str | None,
        bind_mounts: list[tuple[str, str, bool]] | None = None,
        passthrough_paths: list[str] | None = None,
        extra_env: dict[str, str] | None = None,
        extra_init_commands: list[str] | None = None,
        lifecycle_hooks: list[SandboxLifecycleHooks] | None = None,
        modal_options: ModalOptions | None = None,  # noqa: ARG002  # tracked: #288
        attach_accelerator: bool = True,
        ephemeral: bool = False,
        container_image: str | None = None,
        auth_files: list[tuple[str, str]] | None = None,
        resources: Sequence[HostResource] = (),
    ) -> SandboxBackendProtocol:
        # Deferred: the sandbox classes subclass deepagents' BaseSandbox, which
        # pulls langchain + anthropic. Registration must stay import-cheap.
        from vs_sandbox import DockerSandbox  # noqa: PLC0415  # tracked: #288

        # extra_init_commands is accepted for ComputeBackendImpl protocol
        # parity (LocalEnvironment.open() passes it unconditionally) but never
        # used: neither the LOCAL sandbox nor the agent-image-based DOCKER
        # sandbox runs per-launch install commands.
        del attach_accelerator, ephemeral, extra_init_commands
        bind_mounts = list(bind_mounts or [])
        passthrough_paths = list(passthrough_paths or [])
        extra_env = dict(extra_env or {})
        lifecycle_hooks = lifecycle_hooks or []

        if kind is SandboxKind.LOCAL:
            return make_local_shell_sandbox(
                host_workspace=host_workspace,
                env=extra_env,
                lifecycle_hooks=lifecycle_hooks,
            )
        if kind is SandboxKind.DOCKER and self._supports_docker:
            if self.image is None:
                raise ValueError(f"{self.name.value} backend requires a Docker image")  # noqa: TRY003  # tracked: #288
            return DockerSandbox(
                host_workspace=host_workspace,
                # ``DockerEnvironment.open()`` (the plain --docker path)
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
                passthrough_paths=passthrough_paths,
                env=extra_env,
                log_path=log_path,
                auth_files=auth_files,
                lifecycle_hooks=lifecycle_hooks,
            )
        if kind in (SandboxKind.DOCKER, SandboxKind.MODAL):
            raise ValueError(  # noqa: TRY003  # tracked: #288
                f"{self.name.value} backend only supports local execution; "
                f"SandboxKind.{kind.name} is unavailable ({self._unavailable_reason})."
            )
        raise ValueError(f"Unknown sandbox kind: {kind!r}")  # noqa: TRY003  # tracked: #288

    def make_monitor(self, log_dir: Path) -> ContentionMonitor | None:  # noqa: ARG002, D102  # tracked: #288
        return None

    def reselect_device(self) -> None:  # noqa: D102  # tracked: #288
        return None


# Platform-bound constructors — one per local-only backend, registered in
# :mod:`backends` just like the dedicated CUDA/Trainium impl classes. They
# pin the two things a local backend varies: its identity and the message
# shown when Docker/Modal (which can't reach the accelerator) is requested.
# Signatures mirror ``backends.get``'s call convention.


def metal_backend(
    log_dir: Path,
    *,
    log: Callable[[str], None] | None = None,
    image: str | None = None,
) -> LocalBackend:
    """Apple Silicon / Metal backend (local execution only)."""
    return LocalBackend(
        ComputeBackend.METAL,
        log_dir,
        log=log,
        image=image,
        profiler_kind=ProfilerKind.TORCH,
        unavailable_reason=(
            "Docker on macOS can't access Metal/MPS, and Modal does not offer Apple GPUs"
        ),
    )


def cpu_backend(
    log_dir: Path,
    *,
    log: Callable[[str], None] | None = None,
    image: str | None = None,
) -> LocalBackend:
    """CPU-only backend (no GPU; CPU-bound targets like KV stores / servers)."""
    return LocalBackend(
        ComputeBackend.CPU,
        log_dir,
        log=log,
        image=image or _DEFAULT_CPU_IMAGE,
        profiler_kind=ProfilerKind.LINUX_CPU,
        unavailable_reason="Modal CPU execution is not wired up for this backend",
        supports_docker=True,
    )
