"""In-memory :class:`ComputeBackendImpl` test double.

No GPU probing, no container runtime, no subprocess: every sandbox
``make_sandbox`` returns is an in-memory
:class:`~vs_sandbox.api.testing.FakeSandbox`, one per ``(kind, id)`` pair so
a caller that opens more than one sandbox kind gets independent scripts.
``make_monitor``/``reselect_device`` are no-ops, matching a backend with no
contention monitor.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from vs_sandbox.accelerator_discovery import AcceleratorInventory
from vs_sandbox.compute_backends import ComputeBackend
from vs_sandbox.fake_sandbox import FakeSandbox
from vs_sandbox.lifecycle import SandboxLifecycle

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

    from vs_sandbox.compute_backends import ContentionMonitor, SandboxKind
    from vs_sandbox.execution import Sandbox
    from vs_sandbox.host_resources import HostResource
    from vs_sandbox.lifecycle import SandboxLifecycleHooks


class FakeAcceleratorDiscovery:
    """Deterministic in-memory accelerator inventory."""

    def __init__(
        self,
        *,
        trainium: AcceleratorInventory | None = None,
        rocm: AcceleratorInventory | None = None,
    ) -> None:
        """Store deterministic accelerator inventories for both platforms."""
        self._trainium = trainium or AcceleratorInventory()
        self._rocm = rocm or AcceleratorInventory()

    def discover_trainium(self) -> AcceleratorInventory:
        """Return the configured Trainium inventory."""
        return self._trainium

    def discover_rocm(self) -> AcceleratorInventory:
        """Return the configured ROCm inventory."""
        return self._rocm


@dataclass(frozen=True, slots=True)
class FakeSandboxCreation:
    """One sandbox construction observed by :class:`FakeComputeBackend`."""

    kind: SandboxKind
    host_workspace: str
    log_path: Path | str | None
    bind_mounts: tuple[tuple[str, str, bool], ...]
    extra_env: dict[str, str]
    extra_init_commands: tuple[str, ...]
    lifecycle_hooks: tuple[SandboxLifecycleHooks, ...]
    attach_accelerator: bool
    ephemeral: bool
    container_image: str | None
    auth_files: tuple[tuple[str, str], ...]
    resources: tuple[HostResource, ...]


class FakeComputeBackend:
    """Configurable in-memory double for :class:`ComputeBackendImpl`."""

    name = ComputeBackend.CUDA

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
        self.creations: list[FakeSandboxCreation] = []

    def script_sandbox(
        self,
        kind: SandboxKind,
        host_workspace: str,
        sandbox: FakeSandbox,
    ) -> None:
        """Use *sandbox* for the exact kind/workspace construction key."""
        self.sandboxes[f"{kind.value}:{host_workspace}"] = sandbox

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
        self.creations.append(
            FakeSandboxCreation(
                kind=kind,
                host_workspace=host_workspace,
                log_path=log_path,
                bind_mounts=tuple(bind_mounts or ()),
                extra_env=dict(extra_env or {}),
                extra_init_commands=tuple(extra_init_commands or ()),
                lifecycle_hooks=tuple(lifecycle_hooks or ()),
                attach_accelerator=attach_accelerator,
                ephemeral=ephemeral,
                container_image=container_image,
                auth_files=tuple(auth_files or ()),
                resources=tuple(resources),
            )
        )
        key = f"{kind.value}:{host_workspace}"
        sandbox = self.sandboxes.get(key)
        if sandbox is None:
            sandbox = FakeSandbox()
            self.sandboxes[key] = sandbox
        SandboxLifecycle(lifecycle_hooks).before_ready(sandbox)
        return sandbox

    def make_monitor(self, log_dir: Path) -> ContentionMonitor | None:
        """Report no contention monitor."""
        del log_dir
        return None

    def reselect_device(self) -> None:
        """No device to reselect."""
