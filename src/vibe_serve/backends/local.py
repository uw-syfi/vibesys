"""No-device compute backend, shared by Metal and CPU.

Some targets run on the host with no accelerator the sandbox layer can reach:

- ``METAL`` — Apple Silicon: macOS Docker has no Metal/MPS passthrough and
  Modal offers no Apple GPUs.
- ``CPU`` — no GPU at all (CPU-bound targets: KV stores, networking servers).

Both have no device to select, no contention monitor, and no device migration.
Metal remains local-only because Docker/Modal cannot expose MPS. CPU can also
run inside Docker because it needs no accelerator passthrough. Per-platform
*prompt* guidance (MPS vs pure-CPU) lives in the backend fragments under
``templates/_backend/<name>/`` — not here.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from deepagents.backends import LocalShellBackend
from deepagents.backends.sandbox import BaseSandbox

from vibe_serve.backends.base import (
    ContentionMonitor,
    ModalOptions,
    SandboxKind,
    SetupFn,
)
from vibe_serve.constants import ComputeBackend
from vibe_serve.profilers import ProfilerKind
from vibe_serve.sandbox.docker_sandbox import DockerSandbox

_DEFAULT_CPU_IMAGE = "python:3.12-bookworm"


class LocalBackend:
    """No-device backend (Metal / CPU) — hardware hooks are no-ops."""

    profiler_kind = ProfilerKind.TORCH

    def __init__(
        self,
        name: ComputeBackend,
        log_dir: Path,
        *,
        log: Callable[[str], None] | None = None,
        image: str | None = None,
        unavailable_reason: str,
        supports_docker: bool = False,
    ) -> None:
        self.name = name
        self.log_dir = Path(log_dir)
        self._lprint = log or print
        self.image = image
        self._unavailable_reason = unavailable_reason
        self._supports_docker = supports_docker
        # No accelerator to pick — kept for protocol parity with other backends
        # (e.g. _RunContext reads ``selected_device``).
        self.selected_device = None

    # -- ComputeBackendImpl protocol -----------------------------------------

    def make_sandbox(
        self,
        kind: SandboxKind,
        *,
        host_workspace: str,
        log_path: Path | str | None,
        bind_mounts: list[tuple[str, str, bool]] | None = None,
        passthrough_paths: list[str] | None = None,
        extra_env: dict[str, str] | None = None,
        extra_init_commands: list[str] | None = None,
        setup_fns: list[SetupFn] | None = None,
        modal_options: ModalOptions | None = None,
    ) -> BaseSandbox:
        bind_mounts = list(bind_mounts or [])
        passthrough_paths = list(passthrough_paths or [])
        extra_env = dict(extra_env or {})
        extra_init_commands = list(extra_init_commands or [])
        setup_fns = setup_fns or []

        if kind is SandboxKind.LOCAL:
            return LocalShellBackend(
                root_dir=host_workspace,
                virtual_mode=True,
                inherit_env=True,
                env=extra_env,
            )
        if kind is SandboxKind.DOCKER and self._supports_docker:
            if self.image is None:
                raise ValueError(f"{self.name.value} backend requires a Docker image")
            return DockerSandbox(
                host_workspace=host_workspace,
                image=self.image,
                gpus=None,
                bind_mounts=bind_mounts,
                passthrough_paths=passthrough_paths,
                env=extra_env,
                log_path=log_path,
                extra_init_commands=extra_init_commands,
                setup_fns=setup_fns,
            )
        if kind in (SandboxKind.DOCKER, SandboxKind.MODAL):
            raise ValueError(
                f"{self.name.value} backend only supports local execution; "
                f"SandboxKind.{kind.name} is unavailable ({self._unavailable_reason})."
            )
        raise ValueError(f"Unknown sandbox kind: {kind!r}")

    def make_monitor(self, log_dir: Path) -> ContentionMonitor | None:
        return None

    def reselect_device(self) -> None:
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
        unavailable_reason="Modal CPU execution is not wired up for this backend",
        supports_docker=True,
    )
