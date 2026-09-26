"""In-memory :class:`~vibesys.backends.base.ComputeBackendImpl` test double.

No GPU probing, no container runtime, no subprocess: every sandbox
``make_sandbox`` returns is an in-memory
:class:`~vs_sandbox.api.testing.FakeSandbox`, one per ``(kind, id)`` pair so
a caller that opens more than one sandbox kind gets independent scripts.
``make_monitor``/``reselect_device`` are no-ops, matching a backend with no
contention monitor.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from vibesys.constants import ComputeBackend
from vibesys.profilers import ProfilerKind
from vs_sandbox.api.testing import FakeSandbox

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

    from vibesys.backends.base import ContentionMonitor, SandboxKind
    from vs_sandbox.api import HostResource, Sandbox, SandboxLifecycleHooks


class FakeComputeBackend:
    """Configurable in-memory double for :class:`~vibesys.backends.base.ComputeBackendImpl`."""

    name = ComputeBackend.CUDA
    profiler_kind = ProfilerKind.NONE

    def __init__(
        self,
        log_dir: Path | None = None,
        *,
        log: Callable[[str], None] | None = None,
        image: str | None = None,
    ) -> None:
        """Accept the same construction shape as a real backend factory."""
        del log_dir, log, image
        self.sandboxes: dict[str, FakeSandbox] = {}

    def make_sandbox(  # noqa: PLR0913  # LW-040001 [PLR0913]; the parameters are independent injected collaborators or options, and bundling them would hide ownership.
        self,
        kind: SandboxKind,
        *,
        host_workspace: str,
        log_path: Path | str | None = None,
        bind_mounts: list[tuple[str, str, bool]] | None = None,
        extra_env: dict[str, str] | None = None,
        extra_init_commands: list[str] | None = None,
        lifecycle_hooks: list[SandboxLifecycleHooks] | None = None,
        attach_accelerator: bool = True,
        ephemeral: bool = False,
        container_image: str | None = None,
        auth_files: list[tuple[str, str]] | None = None,
        resources: Sequence[HostResource] = (),
    ) -> Sandbox:
        """Return a fresh :class:`FakeSandbox` keyed by *kind* and *host_workspace*."""
        del (
            log_path,
            bind_mounts,
            extra_env,
            extra_init_commands,
            lifecycle_hooks,
            attach_accelerator,
            ephemeral,
            container_image,
            auth_files,
            resources,
        )
        key = f"{kind.value}:{host_workspace}"
        sandbox = self.sandboxes.get(key)
        if sandbox is None:
            sandbox = FakeSandbox()
            self.sandboxes[key] = sandbox
        return sandbox

    def make_monitor(self, log_dir: Path) -> ContentionMonitor | None:
        """Report no contention monitor."""
        del log_dir
        return None

    def reselect_device(self) -> None:
        """No device to reselect."""
