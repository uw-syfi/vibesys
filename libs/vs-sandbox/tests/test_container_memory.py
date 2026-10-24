"""Every container a compute backend starts is capped below the Docker host's memory.

Unbounded, one container's runaway process (four parallel compiler backends of 33 GiB
each, in the case that motivated this) took every byte of a 128 GiB host. The kernel
then thrashed the whole host for 45 minutes before killing anything, and the run's own
process, starved with it, lost its lease and died. Capped, the process is killed inside
its container while the host keeps a reserve.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vs_sandbox.api import (
    MIN_CONTAINER_MEMORY_RESERVE_BYTES,
    ComputeBackend,
    SandboxKind,
    container_memory_limit,
    create_compute_backend,
)
from vs_sandbox.api.testing import FakeDockerEngine
from vs_sim.api import DISCARD
from vs_sim.api.testing import FakePipedProcessLauncher, ManualClock, ScriptedProbe, SimThreads

if TYPE_CHECKING:
    from pathlib import Path

_GIB = 1024**3
_TOTALS = st.integers(min_value=_GIB, max_value=4096 * _GIB)
_DOCKER_BACKENDS = [
    ComputeBackend.CUDA,
    ComputeBackend.ROCM,
    ComputeBackend.TRAINIUM,
    ComputeBackend.CPU,
]


@given(total=_TOTALS)
def test_a_container_never_gets_the_whole_host_and_never_less_than_half(total: int) -> None:
    limit = container_memory_limit(total)

    assert total // 2 <= limit < total
    # The host keeps an eighth of its memory, and at least the fixed reserve when it has
    # room for it beside half the memory.
    assert total - limit >= total // 8
    if total >= 2 * MIN_CONTAINER_MEMORY_RESERVE_BYTES:
        assert total - limit >= MIN_CONTAINER_MEMORY_RESERVE_BYTES


def _started_run(
    tmp_path: Path, backend: ComputeBackend, memory_total: int | None
) -> tuple[str, ...]:
    """The ``docker run`` argv of one Docker sandbox ``backend`` starts on a fake host."""
    engine = FakeDockerEngine(
        tmp_path / "engine",
        agent_ids=(os.getuid(), os.getgid()),
        launcher=FakePipedProcessLauncher(),
    )
    engine.report_memory(memory_total)
    implementation = create_compute_backend(
        backend,
        tmp_path / "logs",
        log=lambda _line: None,
        docker=engine,
        threads=SimThreads(),
        launcher=FakePipedProcessLauncher(),
        clock=ManualClock(),
        probe=ScriptedProbe(lambda _argv: None),
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    sandbox = implementation.make_sandbox(
        SandboxKind.DOCKER,
        host_workspace=str(workspace),
        log_path=None,
        attach_accelerator=False,
        container_image="agent-image",
    )
    sandbox.start(output=DISCARD)
    sandbox.stop()
    runs = [call for call in engine.calls if call[1] == "run"]
    assert len(runs) == 1
    return runs[0]


def _flag(argv: tuple[str, ...], flag: str) -> list[str]:
    return [argv[index + 1] for index, token in enumerate(argv) if token == flag]


@pytest.mark.parametrize("backend", _DOCKER_BACKENDS, ids=str)
@pytest.mark.parametrize("total", [16 * _GIB, 128 * _GIB, 2048 * _GIB], ids=["16G", "128G", "2T"])
def test_every_backend_caps_its_containers_below_the_hosts_memory(
    tmp_path: Path, backend: ComputeBackend, total: int
) -> None:
    run = _started_run(tmp_path, backend, total)

    limit = str(container_memory_limit(total))
    assert _flag(run, "--memory") == [limit]
    # Swap included: on a host with swap, a limit on memory alone would page instead of
    # killing, which starves the host the same way.
    assert _flag(run, "--memory-swap") == [limit]


@pytest.mark.parametrize("backend", _DOCKER_BACKENDS, ids=str)
def test_a_host_that_does_not_report_its_memory_starts_containers_uncapped(
    tmp_path: Path, backend: ComputeBackend
) -> None:
    run = _started_run(tmp_path, backend, None)

    assert "--memory" not in run
    assert "--memory-swap" not in run
