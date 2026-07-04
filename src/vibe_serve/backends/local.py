"""Local-only compute backend, shared by Metal and CPU.

Some targets run on the host with no accelerator the sandbox layer can reach:

- ``METAL`` — Apple Silicon: macOS Docker has no Metal/MPS passthrough and
  Modal offers no Apple GPUs.
- ``CPU`` — no GPU at all (CPU-bound targets: KV stores, networking servers).

Both execute identically — a ``LocalShellBackend`` via ``SandboxKind.LOCAL``,
with no device to select, no contention monitor, and no device migration.
They differ only in identity (``name``) and the message shown when
``DOCKER`` / ``MODAL`` is requested, so both are instances of this one class,
bound to their platform at registration in :mod:`backends`. Per-platform
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


class LocalBackend:
    """Local-only backend (Metal / CPU) — all hardware hooks are no-ops."""

    profiler_kind = "torch"

    def __init__(
        self,
        name: ComputeBackend,
        log_dir: Path,
        *,
        log: Callable[[str], None] | None = None,
        image: str | None = None,
        unavailable_reason: str,
    ) -> None:
        self.name = name
        self.log_dir = Path(log_dir)
        self._lprint = log or print
        self._unavailable_reason = unavailable_reason
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
        if kind is not SandboxKind.LOCAL:
            raise ValueError(
                f"{self.name.value} backend only supports local execution; "
                f"SandboxKind.{kind.name} is unavailable ({self._unavailable_reason})."
            )
        return LocalShellBackend(
            root_dir=host_workspace,
            virtual_mode=True,
            inherit_env=True,
            env=dict(extra_env or {}),
        )

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
        image=image,
        unavailable_reason="there is no GPU to pass through to a Docker/Modal container",
    )
