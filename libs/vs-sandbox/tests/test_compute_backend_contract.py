"""Shared contract for every public ``ComputeBackendImpl``.

Parametrized over the real ``CudaBackend`` (cheap
and real when built with ``attach_accelerator=False``: no ``nvidia-smi``
call, no container runtime, just the unconfined local shell sandbox) and
``FakeComputeBackend`` (in-memory, no subprocess at
all). Both satisfy ``ComputeBackendImpl`` and are used interchangeably by
the application ``backend_factory`` seam.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from vs_sandbox.api import (
    BeforeReadyContext,
    ComputeBackendImpl,
    CudaBackend,
    SandboxKind,
    SandboxLifecycleHooks,
)
from vs_sandbox.api.testing import FakeComputeBackend

if TYPE_CHECKING:
    from pathlib import Path

    from vs_sandbox.api import Sandbox


class _RecordingHooks(SandboxLifecycleHooks):
    def __init__(self) -> None:
        self.sandbox: Sandbox | None = None

    def before_ready(self, context: BeforeReadyContext) -> None:
        self.sandbox = context.sandbox

_FACTORIES = {
    "real": CudaBackend,
    "fake": FakeComputeBackend,
}


@pytest.mark.parametrize("factory_name", sorted(_FACTORIES))
class TestComputeBackendContract:
    """Every ``ComputeBackendImpl``, probed through the same protocol."""

    def test_satisfies_the_protocol(self, factory_name: str, tmp_path: Path) -> None:
        backend = _FACTORIES[factory_name](tmp_path)

        assert isinstance(backend, ComputeBackendImpl)

    def test_make_sandbox_local_without_accelerator_never_touches_a_device(
        self, factory_name: str, tmp_path: Path
    ) -> None:
        backend = _FACTORIES[factory_name](tmp_path)

        sandbox = backend.make_sandbox(
            SandboxKind.LOCAL,
            host_workspace=str(tmp_path),
            log_path=None,
            attach_accelerator=False,
        )

        assert sandbox.id
        result = sandbox.execute("printf hello")
        assert result.exit_code in (0, None)

    def test_make_monitor_and_reselect_device_are_no_ops_without_a_device(
        self, factory_name: str, tmp_path: Path
    ) -> None:
        backend = _FACTORIES[factory_name](tmp_path)

        assert backend.make_monitor(tmp_path) is None
        backend.reselect_device()  # must not raise

    def test_make_sandbox_runs_lifecycle_hooks_before_returning(
        self, factory_name: str, tmp_path: Path
    ) -> None:
        backend = _FACTORIES[factory_name](tmp_path)
        hooks = _RecordingHooks()

        sandbox = backend.make_sandbox(
            SandboxKind.LOCAL,
            host_workspace=str(tmp_path),
            log_path=None,
            lifecycle_hooks=[hooks],
            attach_accelerator=False,
        )

        assert hooks.sandbox is sandbox
