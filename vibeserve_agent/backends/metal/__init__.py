"""Metal backend: Apple Silicon GPU via local execution (no container).

macOS Docker has no Metal/MPS passthrough and Modal doesn't offer
Apple GPUs, so this backend only supports ``SandboxKind.LOCAL``.
``make_sandbox`` raises a clear error for ``DOCKER`` / ``MODAL`` rather
than constructing something that would silently fail.

There's no device selection: Apple Silicon exposes one integrated GPU
via Metal, so ``selected_device`` stays ``None``, ``make_monitor``
returns ``None``, and ``reselect_device`` is a no-op.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from deepagents.backends import LocalShellBackend
from deepagents.backends.sandbox import BaseSandbox

from vibeserve_agent.backends.base import (
    ContentionMonitor,
    ModalOptions,
    SandboxKind,
    SetupFn,
)
from vibeserve_agent.constants import ComputeBackend


class MetalBackend:
    """Apple Silicon / Metal backend (local execution only)."""

    name = ComputeBackend.METAL
    profiler_kind = "torch"

    def __init__(
        self,
        log_dir: Path,
        *,
        log: Callable[[str], None] | None = None,
        image: str | None = None,
    ) -> None:
        self.log_dir = Path(log_dir)
        self._lprint = log or print
        # No GPU selection on Apple Silicon — kept for protocol parity with
        # other backends (e.g. _RunContext reads ``selected_device``).
        self.selected_device = None

    # -- ComputeBackendImpl protocol ---------------------------------------------

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
        if kind is SandboxKind.DOCKER or kind is SandboxKind.MODAL:
            raise ValueError(
                f"metal backend only supports local execution on macOS; "
                f"SandboxKind.{kind.name} is unavailable (Docker on macOS "
                f"can't access Metal/MPS, and Modal does not offer Apple GPUs)."
            )
        if kind is not SandboxKind.LOCAL:
            raise ValueError(f"Unknown sandbox kind: {kind!r}")

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
