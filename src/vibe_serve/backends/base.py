"""Compute backend protocol — the contract every compute target implements.

A ``ComputeBackendImpl`` knows how to:

1. Construct a sandbox configured for its compute platform
   (image, GPU runtime args, env vars are all internal to the backend).
2. Optionally watch the platform for issues (CUDA: nvidia-smi contention).
3. Optionally migrate compute mid-run (CUDA: re-pick a less-loaded GPU).

Sandbox classes (``DockerSandbox``, ``ModalSandbox``, ``LocalShellBackend``)
stay backend-agnostic: they accept image/env/gpus as plain parameters.  The
compute backend supplies the right values for its platform inside
``make_sandbox``.
"""

from __future__ import annotations

from collections.abc import Callable
from enum import StrEnum
from pathlib import Path
from typing import Protocol, runtime_checkable

from deepagents.backends.sandbox import BaseSandbox

from vibe_serve.constants import ComputeBackend
from vibe_serve.profilers import ProfilerKind


class SandboxKind(StrEnum):
    """Where the agent's shell commands actually execute."""

    LOCAL = "local"
    DOCKER = "docker"
    MODAL = "modal"


class Device(Protocol):
    """Minimum device interface ``_RunContext`` consumes for logging and pinning."""

    index: int
    name: str


class ContentionMonitor(Protocol):
    """Background thread that reports platform contention (e.g. shared-GPU use)."""

    def start(self) -> None: ...
    def stop(self) -> None: ...


SetupFn = Callable[[BaseSandbox], None]
"""A function the sandbox runs after every ``start()`` (initial or restart).

Use it to install setup that doesn't survive container restart and that the
sandbox class itself doesn't know about — e.g. ``ln -sfn`` symlinks pointing
into HuggingFace-cache-style bind mounts.
"""


@runtime_checkable
class ComputeBackendImpl(Protocol):
    """Per-platform backend.  See module docstring for the contract."""

    name: ComputeBackend
    profiler_kind: ProfilerKind  # picks profiler support, MCP, and prompt template

    def make_sandbox(
        self,
        kind: SandboxKind,
        *,
        host_workspace: str,
        log_path: Path | str | None,
        bind_mounts: list[tuple[str, str, bool]],
        passthrough_paths: list[str],
        extra_env: dict[str, str],
        extra_init_commands: list[str],
        setup_fns: list[SetupFn] | None = None,
        modal_options: ModalOptions | None = None,
    ) -> BaseSandbox:
        """Construct (do not start) a sandbox configured for this backend.

        ``setup_fns`` are invoked by the sandbox at the end of every
        ``start()`` — initial and restart alike.
        """
        ...

    def make_monitor(self, log_dir: Path) -> ContentionMonitor | None: ...

    def reselect_device(self) -> None:
        """Re-pick the optimal device for this backend (e.g. migrate to a
        less-loaded GPU) and restart affected sandboxes in place.

        Each restarted sandbox re-runs its ``setup_fns`` automatically as
        part of ``start()``.  No-op for backends without rebalancing.
        """
        ...


class ModalOptions:
    """User-supplied Modal sandbox knobs — orthogonal to platform choice.

    The compute backend supplies image and GPU spec; the user supplies runtime
    knobs (lifetime, idle timeout, app, model volume).  Plain attribute
    container so future backends can ignore it without typing gymnastics.
    """

    def __init__(
        self,
        *,
        gpu: str | None = "H100",
        sandbox_timeout: int = 14400,
        idle_timeout: int | None = 1800,
        model_volume_name: str | None = None,
        extra_readonly_volumes: dict[str, str] | None = None,
        extra_writable_volumes: dict[str, str] | None = None,
        app_name: str = "vibeserve",
    ) -> None:
        self.gpu = gpu
        self.sandbox_timeout = sandbox_timeout
        self.idle_timeout = idle_timeout
        self.model_volume_name = model_volume_name
        self.extra_readonly_volumes = extra_readonly_volumes
        self.extra_writable_volumes = extra_writable_volumes
        self.app_name = app_name
