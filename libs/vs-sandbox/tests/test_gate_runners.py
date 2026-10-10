"""The host runners behind the broker's gate operation, driven with a fake cluster.

``srun`` and the Slurm wrapper are replaced by small Python programs at the
process boundary the runners already expose, so cancellation and argv assembly
are real while nothing needs a cluster.
"""

from __future__ import annotations

import os
import sys
import threading
from typing import TYPE_CHECKING

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from vs_sandbox.api import ProjectPathPolicy, SandboxUnavailableError
from vs_sandbox.api.slurm import (
    GateKind,
    GpuCommand,
    GpuJobRequest,
    HostJobConfinement,
    SlurmCommandGateRunner,
    SrunGateRunner,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

_WAIT_FOR_SIGTERM = """\
import io, os, signal, sys

def on_sigterm(*_):
    # os.write, not print: the signal can land inside a print already in progress,
    # and re-entering the buffered stdout there raises instead of printing.
    os.write(1, b"cancelled\\n")
    sys.exit(143)

signal.signal(signal.SIGTERM, on_sigterm)
# The wakeup fd gets a byte when the signal arrives, even if that is before the
# wrapper parks, so waiting on it cannot miss the signal the way signal.pause can.
wake_read, wake_write = os.pipe()
os.set_blocking(wake_write, False)
signal.set_wakeup_fd(wake_write)

class Raw(io.RawIOBase):
    # The first write reports "started", then parks inside that write until the
    # signal arrives, so SIGTERM always lands mid-print, the interleaving that a
    # busy machine only sometimes produces.
    parked = False

    def writable(self):
        return True

    def write(self, data):
        count = os.write(1, data)
        if not Raw.parked:
            Raw.parked = True
            os.read(wake_read, 1)
        return count

sys.stdout = io.TextIOWrapper(io.BufferedWriter(Raw()))
print("started", flush=True)
"""
_ECHO_ARGV = "import os, sys; print(os.getcwd(), *sys.argv[1:])"


class _RecordingLauncher:
    def __init__(self) -> None:
        self.calls: list[tuple[GpuJobRequest, GpuCommand]] = []

    def run(
        self,
        request: GpuJobRequest,
        command: GpuCommand,
        *,
        write: Callable[[bytes], None],
        cancel: threading.Event,
    ) -> int:
        del cancel
        self.calls.append((request, command))
        write(b"ran\n")
        return 4


class TestSlurmCommandGateRunner:
    def test_streams_the_wrappers_output_and_returns_its_status(self, tmp_path: Path) -> None:
        script = tmp_path / "wrapper.py"
        script.write_text(_ECHO_ARGV)
        output = bytearray()
        runner = SlurmCommandGateRunner(
            tmp_path / "plan.json", env=dict(os.environ), wrapper=(sys.executable, str(script))
        )

        status = runner.run(
            GateKind.ACCURACY, (), cwd=tmp_path, write=output.extend, cancel=threading.Event()
        )

        assert status == 0
        assert output.decode().split()[0] == str(tmp_path)

    def test_cancelling_stops_the_wrapper_with_sigterm_so_it_can_cancel_its_job(
        self, tmp_path: Path
    ) -> None:
        script = tmp_path / "wrapper.py"
        script.write_text(_WAIT_FOR_SIGTERM)
        output = bytearray()
        cancel = threading.Event()

        def write(chunk: bytes) -> None:
            output.extend(chunk)
            cancel.set()  # the wrapper is up: ask it to stop

        runner = SlurmCommandGateRunner(
            tmp_path / "plan.json", env=dict(os.environ), wrapper=(sys.executable, str(script))
        )

        status = runner.run(GateKind.BENCHMARK, (), cwd=tmp_path, write=write, cancel=cancel)

        assert status == 143
        assert b"cancelled" in output

    def test_the_wrapper_is_given_the_plan_the_gate_and_the_validated_arguments(
        self, tmp_path: Path
    ) -> None:
        script = tmp_path / "wrapper.py"
        script.write_text("import sys; print(*sys.argv[1:])")
        output = bytearray()
        runner = SlurmCommandGateRunner(
            tmp_path / "plan.json", env=dict(os.environ), wrapper=(sys.executable, str(script))
        )

        runner.run(
            GateKind.BENCHMARK,
            ("--out", "x.json"),
            cwd=tmp_path,
            write=output.extend,
            cancel=threading.Event(),
        )

        assert output.decode().split() == [
            "--plan",
            str(tmp_path / "plan.json"),
            "benchmark",
            "--out",
            "x.json",
        ]


class TestSrunGateRunner:
    @settings(
        max_examples=30, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
    )
    @given(
        arguments=st.lists(st.text(alphabet="abc-./", min_size=1, max_size=6), max_size=3),
        gate=st.sampled_from(list(GateKind)),
    )
    def test_runs_the_planned_argv_with_the_arguments_appended_in_the_gate_allocation(
        self, tmp_path: Path, arguments: list[str], gate: GateKind
    ) -> None:
        launcher = _RecordingLauncher()
        request = GpuJobRequest(gpus=2, time_minutes=40)
        planned = {
            GateKind.ACCURACY: ("python", "accuracy.py"),
            GateKind.BENCHMARK: ("python", "bench.py"),
        }
        runner = SrunGateRunner(launcher, request, planned, env={"A": "1"})
        output = bytearray()

        status = runner.run(
            gate, arguments, cwd=tmp_path, write=output.extend, cancel=threading.Event()
        )

        (called_request, command) = launcher.calls[-1]
        assert status == 4
        assert called_request == request
        assert command.argv == (*planned[gate], *arguments)
        assert command.cwd == tmp_path
        assert dict(command.env) == {"A": "1"}

    def test_a_gate_the_run_did_not_plan_exits_with_a_usage_status(self, tmp_path: Path) -> None:
        launcher = _RecordingLauncher()
        runner = SrunGateRunner(
            launcher,
            GpuJobRequest(gpus=1, time_minutes=1),
            {GateKind.ACCURACY: ("true",)},
            env={},
        )
        output = bytearray()

        status = runner.run(
            GateKind.BENCHMARK, (), cwd=tmp_path, write=output.extend, cancel=threading.Event()
        )

        assert status == 2
        assert b"not configured" in output
        assert launcher.calls == []


class TestHostJobConfinement:
    def test_a_host_that_cannot_enforce_confinement_refuses_the_job(self, tmp_path: Path) -> None:
        """Enforcement is required: there is no unconfined job."""
        confinement = HostJobConfinement(
            env={"VIBESYS_AGENT_SANDBOX": "0"},
            resources=(),
            project_path_policy=ProjectPathPolicy(),
        )

        with pytest.raises(SandboxUnavailableError):
            confinement.wrap(tmp_path, ["true"])
