"""Shared contract for every :class:`~vibesys.backends.base.ComputeBackendImpl`.

Parametrized over the real :class:`~vibesys.backends.cuda.CudaBackend` (cheap
and real when built with ``attach_accelerator=False``: no ``nvidia-smi``
call, no container runtime, just the unconfined local shell sandbox) and
:class:`~vibesys.api.testing.FakeComputeBackend` (in-memory, no subprocess at
all). Both satisfy ``ComputeBackendImpl`` and are used interchangeably by
``vibesys.context``'s ``backend_factory`` seam.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from vibesys.api.testing import FakeComputeBackend
from vibesys.backends.base import ComputeBackendImpl, SandboxKind
from vibesys.backends.cuda import CudaBackend

if TYPE_CHECKING:
    from pathlib import Path

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
