"""CPU backend: native-CPU / compiled-engine target via local execution.

The candidate for this backend is a **compiled binary** (e.g. a Rust engine
built with ``cargo build --release``) run as a subprocess — not a torch model
on a GPU. There is no device to select and no GPU profiler; performance is
measured by the example's benchmark harness (``benchmark/benchmark.py``), which
runs the binary and reports ``events_per_sec``.

Local-exec only for now (like ``MetalBackend``): ``make_sandbox`` raises a clear
error for ``DOCKER`` / ``MODAL`` rather than constructing something that would
silently fail. A portable CPU-container branch (which *would* be sound for a pure
CPU workload) is a deliberate future hook — see the ``TODO`` in ``make_sandbox``.

With no device selection, ``selected_device`` stays ``None``, ``make_monitor``
returns ``None``, and ``reselect_device`` is a no-op.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from deepagents.backends import LocalShellBackend
from deepagents.backends.sandbox import BaseSandbox

from vibe_database.backends.base import (
    ContentionMonitor,
    ModalOptions,
    SandboxKind,
    SetupFn,
)
from vibe_database.constants import ComputeBackend


class CpuBackend:
    """Native-CPU / compiled-engine backend (local execution only)."""

    name = ComputeBackend.CPU
    # No GPU profiler for a compiled CPU engine; perf comes from the benchmark
    # harness. ``native`` selects the profiler-free ``profiler_prompt_native.j2``
    # prompt (run the benchmark, read ``events_per_sec``) instead of the GPU
    # torch/nsys prompts.
    profiler_kind = "native"

    def __init__(
        self,
        log_dir: Path,
        *,
        log: Callable[[str], None] | None = None,
        image: str | None = None,
    ) -> None:
        self.log_dir = Path(log_dir)
        self._lprint = log or print
        # No device selection on a plain CPU target — kept for protocol parity
        # with other backends (e.g. _RunContext reads ``selected_device``).
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
            # TODO: a CPU workload is portable, so a plain CPU-container branch
            # (no --gpus / --device passthrough) would be sound here — wire it
            # up when a containerized CPU run is needed. Local-only for now.
            raise ValueError(
                f"cpu backend only supports local execution for now; "
                f"SandboxKind.{kind.name} is not yet wired up (a portable "
                f"CPU-container branch is a future hook)."
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
