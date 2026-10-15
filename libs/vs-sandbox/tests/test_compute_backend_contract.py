"""Shared contract for every public ``ComputeBackendImpl``.

Parametrized over the real ``CudaBackend`` (cheap
and real when built with ``attach_accelerator=False``: no ``nvidia-smi``
call, no container runtime, just the unconfined local shell sandbox) and
``FakeComputeBackend`` (in-memory, no subprocess at
all). Both satisfy ``ComputeBackendImpl`` and are used interchangeably by
the application ``backend_factory`` seam.
"""

from __future__ import annotations

import inspect
from functools import partial
from io import StringIO
from typing import TYPE_CHECKING

import pytest

from vs_sandbox.api import (
    BeforeReadyContext,
    ComputeBackendImpl,
    CudaBackend,
    LocalBackend,
    RocmBackend,
    SandboxKind,
    SandboxLifecycleHooks,
    TrainiumBackend,
)
from vs_sandbox.api.testing import FakeComputeBackend, ScriptedDockerCli
from vs_sim.api import OsThreads
from vs_sim.api.testing import (
    FakePipedProcessLauncher,
    ManualClock,
    PipedStart,
    ScriptedProbe,
    SimThreads,
)

if TYPE_CHECKING:
    from pathlib import Path

    from vs_sandbox.api import CommandRunner
    from vs_sim.api import PipedProcess


class _RecordingHooks(SandboxLifecycleHooks):
    def __init__(self) -> None:
        self.sandbox: CommandRunner | None = None

    def before_ready(self, context: BeforeReadyContext) -> None:
        self.sandbox = context.sandbox


class _FinishedShell:
    """A host shell that already printed its scripted output and exited 0."""

    def __init__(self, stdout: str) -> None:
        self.stdout = StringIO(stdout)
        self.stderr = StringIO()

    def wait(self, timeout: float | None) -> int:
        del timeout
        return 0

    def terminate_group(self) -> None:
        raise AssertionError

    def kill_group(self) -> None:
        raise AssertionError

    def cancel_output(self) -> None:
        raise AssertionError


def _printf_shell(start: PipedStart) -> PipedProcess:
    """Run the one host command the contract issues: ``printf hello`` under ``/bin/sh``."""
    assert start.argv == ("/bin/sh", "-c", "printf hello"), start.argv
    return _FinishedShell("hello")


_FACTORIES = {
    "real": partial(
        CudaBackend,
        clock=ManualClock(),
        docker=ScriptedDockerCli(),
        # The scripted shell exits at once, so its output drains never wait on time.
        threads=OsThreads(),
        launcher=FakePipedProcessLauncher(_printf_shell),
        probe=ScriptedProbe(lambda _argv: None),
    ),
    "fake": partial(FakeComputeBackend, threads=SimThreads()),
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


@pytest.mark.parametrize(
    "backend_class", [CudaBackend, RocmBackend, TrainiumBackend, LocalBackend, FakeComputeBackend]
)
def test_make_sandbox_promises_nothing_an_implementation_would_drop(
    backend_class: type[ComputeBackendImpl],
) -> None:
    """`extra_init_commands` was accepted and ignored by every backend; it must stay gone.

    Root-only setup in a started container is the caller's explicit
    `execute_as_root`, which works the same on every backend.
    """
    parameters = inspect.signature(backend_class.make_sandbox).parameters
    protocol = inspect.signature(ComputeBackendImpl.make_sandbox).parameters

    assert "extra_init_commands" not in parameters
    assert set(parameters) == set(protocol)


@pytest.mark.parametrize(
    "backend_class", [CudaBackend, RocmBackend, TrainiumBackend, LocalBackend, FakeComputeBackend]
)
def test_every_backend_declares_ensure_accelerator_available(
    backend_class: type[ComputeBackendImpl],
) -> None:
    """Every backend accepts the pre-image-build host-capability check (#2093).

    Not called here: the real ``CudaBackend`` would shell out to ``nvidia-smi``,
    which this deterministic suite never does. ``test_cuda_backend.py`` exercises
    the raising behavior itself with a fake telemetry.
    """
    assert callable(backend_class.ensure_accelerator_available)
